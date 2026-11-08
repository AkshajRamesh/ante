"""Build, reconcile and snapshot the Gold layer (workspace.ante_gold) for the ante dashboard.

Gold combines two sources:
  - Silver tables (workspace.ante_silver) for additive, filterable facts recomputed in SQL
  - the validated Python analysis outputs (outputs/*.csv), uploaded verbatim to a Gold volume, as the
    authoritative source for statistical estimates, forecasts and scenarios

Every Gold table is rebuilt with CREATE OR REPLACE, so reruns are idempotent. Bronze, Silver and the
analysis outputs are read only. Reconciliation checks compare SQL results with the Python outputs and
fail visibly on mismatches. A sanitized snapshot of every Gold table is exported for the public app.

usage: python scripts/build-gold.py [--profile ante] [--checks-only] [--no-snapshot] [--workers 4]
"""
import argparse
import csv
import decimal
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("load_bronze", ROOT / "scripts" / "load-bronze.py")
bronze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bronze)

S = "workspace.ante_silver"
G = "workspace.ante_gold"
VOLUME = "workspace.ante_gold.analysis_outputs"
VOLUME_PATH = "/Volumes/workspace/ante_gold/analysis_outputs"
OUTPUTS = ROOT / "outputs"
SNAPSHOT = ROOT / "snapshot"
QUALITY_AUDIT = ROOT / "audit" / "gold-quality-audit.csv"
LINEAGE_AUDIT = ROOT / "audit" / "gold-lineage.csv"
SILVER_AUDIT = ROOT / "audit" / "silver-quality-audit.csv"
BUDGET_USD = 100000
MIN_CELL = 20
FORBIDDEN_COLUMNS = ["player_id", "observed_visit_id", "session_segment_id", "loyalty_event_id", "transaction_id",
                     "comp_id", "redeemed_pos_id", "source_segment_id", "assignment_id", "incident_id"]
SNAPSHOT_PAGE = 20000

SCOPE_FILTERABLE = ("filterable: additive daily sums; compute ratios as ratio of sums over the filtered rows")
SCOPE_WINDOW = ("fixed: whole floor, gaming days 2025-01-01 to 2025-06-30; computed once by the Python analysis "
                "and not recomputed by dashboard filters")
SCOPE_FORECAST = ("fixed: frozen whole-property forecast of daily coin-in made 2025-04-30 for 2025-05-01 to "
                  "2025-06-30; not filterable by zone, machine or game")
SCOPE_PLAN = ("fixed: July 1-30 2025 plan horizon; values are expected win (theo) scenarios, not realized win")
SCOPE_SEGMENTS = (f"filterable by month and one pre-play guest attribute; cells with fewer than {MIN_CELL} players "
                  "are suppressed")

#analysis script that produced each output, for the freshness check
SCRIPT_PREFIXES = {
    "analyze-demand.py": ["demand-"], "analyze-downtime.py": ["downtime-"], "analyze-forecast.py": ["forecast-"],
    "analyze-investments.py": ["investment-"], "analyze-loyalty.py": ["loyalty-"], "analyze-players.py": ["players-"],
    "analyze-machine-performance.py": ["machine-performance", "machine-placement-periods", "zone-summary",
                                       "replacement-slots", "m-1158-monthly", "config-hit-rates",
                                       "baseline-model-coefficients", "validation-checks", "source-files",
                                       "metric-definitions"],
}


def lit(text):
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def src(name):
    #verbatim python output; pandas writes doubled quotes, so escape is the quote character
    return (f"read_files('{VOLUME_PATH}/{name}', format => 'csv', header => true, inferSchema => true, "
            f"escape => '\"', mode => 'FAILFAST')")


CLOSURE_CTE = (
    f"closure AS (SELECT MIN(cf.gaming_date) AS closure_start, MAX(ct.gaming_date) AS closure_end "
    f"FROM {S}.floor_walkways w "
    f"JOIN {S}.calendar_day cf ON w.closed_from_utc >= cf.gaming_day_start_utc AND w.closed_from_utc < cf.gaming_day_end_utc "
    f"JOIN {S}.calendar_day ct ON w.closed_to_utc - INTERVAL 1 SECOND >= ct.gaming_day_start_utc "
    f"AND w.closed_to_utc - INTERVAL 1 SECOND < ct.gaming_day_end_utc WHERE w.closed_from_utc IS NOT NULL)")


def period_case(column):
    return (f"CASE WHEN {column} < c.closure_start THEN 'pre' WHEN {column} <= c.closure_end THEN 'closure' "
            f"ELSE 'post' END")


#=== gold tables ===

TABLES = []


def table(name, question, grain, keys, scope, sql, sources, wave=1):
    TABLES.append({"name": name, "question": question, "grain": grain, "keys": keys, "scope": scope, "sql": sql,
                   "sources": sources, "wave": wave})


table("gold_machine_daily", "Q1, Q2, Q3", "machine x gaming date (on-floor days only)", ["machine_id", "gaming_date"],
      SCOPE_FILTERABLE, f"""
WITH {CLOSURE_CTE}
SELECT p.gaming_date, p.month, p.local_weekday, p.is_weekend, p.federal_holiday, p.events_active,
  {period_case('p.gaming_date')} AS closure_period,
  p.machine_id, p.location_id, p.bank_id, p.zone_id, p.zone_name, p.config_id, p.theme, p.manufacturer, p.lease_class,
  p.volatility_class, p.configured_rtp,
  p.coin_in_usd, p.payout_usd, p.house_win_usd, p.theo_usd, p.luck_residual_usd,
  p.carded_coin_in_usd, CAST(m.carded_theo_cents / 100 AS DECIMAL(20, 4)) AS carded_theo_usd,
  p.lease_cost_usd, p.service_cost_usd, p.power_cost_usd, p.repair_cost_usd,
  p.contribution_after_supplied_costs_usd AS contribution_usd,
  m.scheduled_seconds / 3600D AS scheduled_hours, p.available_hours, p.occupied_hours,
  (m.scheduled_seconds - m.available_seconds) / 3600D AS downtime_hours, p.faults_opened,
  m.paid_spins, m.payout_positive_spins AS hit_spins,
  CAST(m.paid_spins * g.hit_probability AS DOUBLE) AS expected_hit_spins,
  CAST(m.paid_spins * g.hit_probability * (1 - g.hit_probability) AS DOUBLE) AS hit_variance,
  m.sum_bet_squared * CAST(g.payout_multiplier_variance AS DOUBLE) / 10000 AS luck_variance_usd2
FROM {S}.prepared_daily_view p
JOIN {S}.machine_day m ON m.machine_id = p.machine_id AND m.gaming_date = p.gaming_date
JOIN {S}.game_math g ON g.config_id = p.config_id
CROSS JOIN closure c
WHERE p.active""", ["silver.prepared_daily_view", "silver.machine_day", "silver.game_math", "silver.floor_walkways",
                     "silver.calendar_day"])

table("gold_machine_scorecard", "Q1, Q3", "machine (full window)", ["machine_id"], SCOPE_WINDOW, f"""
WITH mp AS (SELECT * FROM {src('machine-performance.csv')}),
dm AS (SELECT * FROM {src('downtime-machines.csv')}),
base AS (
SELECT CAST(mp.machine_id AS STRING) AS machine_id, mp.`zone` AS primary_zone, CAST(mp.moved AS BOOLEAN) AS moved,
  mp.first_location_id, mp.last_location_id, mp.last_bank_id, mp.config_id, mp.theme, mp.manufacturer, mp.lease_class,
  mp.volatility, CAST(mp.configured_rtp AS DOUBLE) AS configured_rtp, CAST(mp.hit_probability AS DOUBLE) AS hit_probability,
  mp.lifecycle_status, CAST(mp.install_date AS DATE) AS install_date, CAST(mp.retire_date AS DATE) AS retire_date,
  CAST(mp.first_active_date AS DATE) AS first_active_date, CAST(mp.last_active_date AS DATE) AS last_active_date,
  CAST(mp.on_floor_at_window_end AS BOOLEAN) AS on_floor_at_window_end, CAST(mp.days_on_floor AS INT) AS days_on_floor,
  CAST(mp.scheduled_hours AS DOUBLE) AS scheduled_hours, CAST(mp.available_hours AS DOUBLE) AS available_hours,
  CAST(mp.occupied_hours AS DOUBLE) AS occupied_hours, CAST(mp.availability AS DOUBLE) AS availability,
  CAST(mp.occupancy AS DOUBLE) AS occupancy, CAST(mp.paid_spins AS BIGINT) AS paid_spins,
  CAST(mp.payout_positive_spins AS BIGINT) AS hit_spins,
  CAST(mp.coin_in_usd AS DOUBLE) AS coin_in_usd, CAST(mp.house_win_usd AS DOUBLE) AS house_win_usd,
  CAST(mp.theo_usd AS DOUBLE) AS theo_usd, CAST(mp.luck_residual_usd AS DOUBLE) AS luck_residual_usd,
  CAST(mp.contribution_usd AS DOUBLE) AS contribution_usd, CAST(mp.observed_hold AS DOUBLE) AS observed_hold,
  CAST(mp.theo_hold AS DOUBLE) AS theo_hold, CAST(mp.house_win_per_day AS DOUBLE) AS house_win_per_day,
  CAST(mp.contribution_per_day AS DOUBLE) AS contribution_per_day,
  CAST(mp.coin_in_per_avail_hour AS DOUBLE) AS coin_in_per_avail_hour,
  CAST(mp.theo_per_avail_hour AS DOUBLE) AS theo_per_avail_hour,
  CAST(mp.house_win_per_avail_hour AS DOUBLE) AS house_win_per_avail_hour,
  CAST(mp.zone_rank_theo_per_hour AS INT) AS zone_rank_theo_per_hour, CAST(mp.zone_size AS INT) AS zone_size,
  CAST(mp.baseline_theo_per_avail_hour AS DOUBLE) AS baseline_theo_per_avail_hour,
  CAST(mp.demand_index AS DOUBLE) AS demand_index, CAST(mp.actual_win_index AS DOUBLE) AS actual_win_index,
  CAST(mp.luck_sd_usd AS DOUBLE) AS luck_sd_usd, CAST(mp.luck_z AS DOUBLE) AS luck_z,
  CAST(mp.luck_p_bonferroni AS DOUBLE) AS luck_p_bonferroni, CAST(mp.hit_rate AS DOUBLE) AS hit_rate,
  CAST(mp.hit_z AS DOUBLE) AS hit_z,
  CAST(mp.rank_total_house_win AS INT) AS rank_total_house_win, CAST(mp.rank_house_win_per_day AS INT) AS rank_house_win_per_day,
  CAST(mp.rank_theo_per_hour AS INT) AS rank_theo_per_hour, CAST(mp.rank_demand_index AS INT) AS rank_demand_index,
  CAST(mp.rank_actual_win_index AS INT) AS rank_actual_win_index,
  CAST(mp.rank_contribution_per_day AS INT) AS rank_contribution_per_day, CAST(mp.rank_luck_z AS INT) AS rank_luck_z,
  CAST(mp.in_original_bottom_10 AS BOOLEAN) AS in_original_bottom_10,
  CAST(mp.in_demand_bottom_10 AS BOOLEAN) AS in_demand_bottom_10,
  CAST(mp.in_actual_win_bottom_10 AS BOOLEAN) AS in_actual_win_bottom_10,
  CAST(dm.downtime_hours AS DOUBLE) AS downtime_hours,
  CAST(dm.downtime_fault_waiting_hours AS DOUBLE) AS downtime_fault_waiting_hours,
  CAST(dm.faults AS INT) AS faults, CAST(dm.expected_faults AS DOUBLE) AS expected_faults,
  CAST(dm.fault_excess_z AS DOUBLE) AS fault_excess_z, CAST(dm.repeat_faults AS INT) AS repeat_faults,
  CAST(dm.mean_repair_hours AS DOUBLE) AS mean_repair_hours, CAST(dm.direct_cost_usd AS DOUBLE) AS repair_direct_cost_usd,
  CAST(dm.lost_theo_usd AS DOUBLE) AS est_gross_lost_theo_usd
FROM mp LEFT JOIN dm ON dm.machine_id = mp.machine_id)
SELECT *,
  abs(hit_z) > 4 AS flag_payout_pattern,
  luck_p_bonferroni < 0.05 AS flag_luck_unlikely,
  coalesce(fault_excess_z >= 3, false) AS flag_excess_faults,
  in_demand_bottom_10 AS flag_low_demand,
  days_on_floor < 181 AS flag_partial_window,
  abs(hit_z) > 4 OR luck_p_bonferroni < 0.05 OR coalesce(fault_excess_z >= 3, false) AS requires_investigation,
  concat_ws('; ',
    CASE WHEN abs(hit_z) > 4 THEN 'hit rate far from published hit probability (|hit z| > 4): inspect payout configuration' END,
    CASE WHEN luck_p_bonferroni < 0.05 THEN 'house win vs theo unlikely under published math (bonferroni p < 0.05)' END,
    CASE WHEN coalesce(fault_excess_z >= 3, false) THEN 'faults well above its manufacturer rate (fault z >= 3)' END,
    CASE WHEN in_demand_bottom_10 THEN 'low play for its zone and game (demand bottom 10): review position, not the machine' END,
    CASE WHEN days_on_floor < 181 THEN 'partial window: totals cover fewer than 181 days' END) AS review_notes,
  {lit(SCOPE_WINDOW)} AS scope
FROM base""", ["outputs/machine-performance.csv", "outputs/downtime-machines.csv"])

table("gold_bottom_ten", "Q1", "ranking list x position", ["list_name", "list_rank"], SCOPE_WINDOW, f"""
WITH lists AS (
  SELECT 'original: total house win' AS list_name, 'none (manager list)' AS adjustment, 'house_win_usd' AS metric_name,
    house_win_usd AS metric_value, * FROM {G}.gold_machine_scorecard WHERE in_original_bottom_10
  UNION ALL
  SELECT 'exposure-adjusted: theo per available hour', 'exposure and luck (expected win per available hour)',
    'theo_per_avail_hour', theo_per_avail_hour, * FROM {G}.gold_machine_scorecard WHERE rank_theo_per_hour <= 10
  UNION ALL
  SELECT 'demand-adjusted: demand index', 'exposure, luck, zone and game (play vs zone+config baseline)',
    'demand_index', demand_index, * FROM {G}.gold_machine_scorecard WHERE in_demand_bottom_10
  UNION ALL
  SELECT 'actual-win-adjusted: actual win index', 'exposure, zone and game; keeps luck and payout deviations',
    'actual_win_index', actual_win_index, * FROM {G}.gold_machine_scorecard WHERE in_actual_win_bottom_10)
SELECT list_name, adjustment, ROW_NUMBER() OVER (PARTITION BY list_name ORDER BY metric_value, machine_id) AS list_rank,
  machine_id, primary_zone, config_id, theme, lifecycle_status, on_floor_at_window_end, days_on_floor, metric_name,
  metric_value, house_win_usd, theo_usd, luck_residual_usd, luck_z, availability, theo_per_avail_hour, demand_index,
  actual_win_index, in_original_bottom_10, flag_payout_pattern, requires_investigation, review_notes,
  {lit(SCOPE_WINDOW)} AS scope
FROM lists""", ["gold.gold_machine_scorecard"], wave=2)

table("gold_zone_daily", "Q2, Q3", "zone x gaming date", ["zone_id", "gaming_date"], SCOPE_FILTERABLE, f"""
WITH {CLOSURE_CTE},
zones AS (SELECT zone_id, name AS zone_name FROM {S}.floor_locations WHERE node_type = 'zone'),
m AS (SELECT zone_id, gaming_date, COUNT(*) AS machines, SUM(coin_in_usd) AS coin_in_usd, SUM(payout_usd) AS payout_usd,
  SUM(house_win_usd) AS house_win_usd, SUM(theo_usd) AS theo_usd, SUM(luck_residual_usd) AS luck_residual_usd,
  SUM(carded_coin_in_usd) AS carded_coin_in_usd, SUM(scheduled_hours) AS scheduled_machine_hours,
  SUM(available_hours) AS available_hours, SUM(occupied_hours) AS occupied_hours, SUM(downtime_hours) AS downtime_hours,
  SUM(faults_opened) AS faults_opened, SUM(paid_spins) AS paid_spins
  FROM {G}.gold_machine_daily GROUP BY zone_id, gaming_date),
t AS (SELECT zone_id, gaming_date, SUM(entries_count) AS entries_observed, SUM(coverage_minutes) / 60.0 AS covered_sensor_hours,
  COUNT_IF(counter_status = 'ok') AS ok_sensor_hours, COUNT_IF(counter_status = 'partial') AS partial_sensor_hours,
  COUNT_IF(counter_status = 'offline') AS offline_sensor_hours
  FROM {S}.zone_traffic_hourly GROUP BY zone_id, gaming_date)
SELECT cal.gaming_date, cal.month, cal.local_weekday, cal.is_weekend, cal.federal_holiday, pd.events_active,
  {period_case('cal.gaming_date')} AS closure_period, z.zone_id, z.zone_name,
  coalesce(m.machines, 0) AS machines, m.coin_in_usd, m.payout_usd, m.house_win_usd, m.theo_usd, m.luck_residual_usd,
  m.carded_coin_in_usd, m.scheduled_machine_hours, m.available_hours, m.occupied_hours, m.downtime_hours,
  m.faults_opened, m.paid_spins, t.entries_observed, t.covered_sensor_hours,
  cal.scheduled_seconds / 3600.0 AS scheduled_sensor_hours, t.ok_sensor_hours, t.partial_sensor_hours,
  t.offline_sensor_hours
FROM {S}.calendar_day cal CROSS JOIN zones z CROSS JOIN closure c
JOIN {S}.property_day pd ON pd.gaming_date = cal.gaming_date
LEFT JOIN m ON m.zone_id = z.zone_id AND m.gaming_date = cal.gaming_date
LEFT JOIN t ON t.zone_id = z.zone_id AND t.gaming_date = cal.gaming_date""",
      ["gold.gold_machine_daily", "silver.zone_traffic_hourly", "silver.floor_locations", "silver.calendar_day",
       "silver.property_day", "silver.floor_walkways"], wave=2)

table("gold_property_daily", "Q2, Q3, Q5", "gaming date (whole property)", ["gaming_date"], SCOPE_FILTERABLE, f"""
WITH {CLOSURE_CTE},
md AS (SELECT gaming_date, SUM(scheduled_hours) AS scheduled_machine_hours, SUM(downtime_hours) AS downtime_hours,
  SUM(carded_coin_in_usd) AS carded_coin_in_usd, SUM(carded_theo_usd) AS carded_theo_usd
  FROM {G}.gold_machine_daily GROUP BY gaming_date),
ll AS (SELECT event_gaming_date AS gaming_date, COUNT_IF(event_type = 'redeem') AS redemptions,
  SUM(CASE WHEN event_type = 'redeem' THEN -points_delta ELSE 0 END) AS points_redeemed,
  SUM(CASE WHEN event_type = 'award' THEN points_delta ELSE 0 END) AS points_earned
  FROM {S}.loyalty_ledger WHERE event_gaming_date IS NOT NULL GROUP BY event_gaming_date),
fb AS (SELECT gaming_date, COUNT_IF(is_comp) AS comp_tickets,
  COUNT_IF(NOT is_comp AND player_id IS NOT NULL) AS carded_cash_tickets,
  SUM(CASE WHEN NOT is_comp AND player_id IS NOT NULL THEN cash_receipt_cents ELSE 0 END) / 100 AS carded_cash_usd,
  COUNT_IF(player_id IS NULL) AS uncarded_tickets,
  SUM(CASE WHEN player_id IS NULL THEN cash_receipt_cents ELSE 0 END) / 100 AS uncarded_cash_usd
  FROM {S}.fnb_transactions GROUP BY gaming_date),
dd AS (SELECT * FROM {src('downtime-daily.csv')})
SELECT pd.gaming_date, cal.month, cal.local_weekday, cal.is_weekend, cal.federal_holiday, pd.events_active,
  {period_case('pd.gaming_date')} AS closure_period,
  CASE WHEN pd.gaming_date >= DATE'2025-05-01' THEN 'after change' ELSE 'before change' END AS comp_policy_period,
  pd.active_machines, pd.coin_in_usd, pd.payout_usd, pd.house_win_usd, pd.theo_usd,
  pd.house_win_usd - pd.theo_usd AS luck_residual_usd, md.carded_coin_in_usd, md.carded_theo_usd,
  md.scheduled_machine_hours, pd.available_machine_hours, pd.occupied_machine_hours, md.downtime_hours,
  pd.faults_opened,
  CAST(dd.downtime_fault_repair_hours AS DOUBLE) AS downtime_fault_repair_hours,
  CAST(dd.downtime_fault_waiting_hours AS DOUBLE) AS downtime_fault_waiting_hours,
  CAST(dd.downtime_move_hours AS DOUBLE) AS downtime_move_hours,
  CAST(dd.downtime_scheduled_maintenance_hours AS DOUBLE) AS downtime_scheduled_maintenance_hours,
  CAST(dd.downtime_install_hours AS DOUBLE) AS downtime_install_hours,
  CAST(dd.downtime_retire_hours AS DOUBLE) AS downtime_retire_hours,
  CAST(dd.lost_coin_usd AS DOUBLE) AS est_gross_lost_coin_in_usd, CAST(dd.lost_theo_usd AS DOUBLE) AS est_gross_lost_theo_usd,
  pd.pos_cash_usd, pd.pos_cogs_usd, pd.comp_cost_usd, pd.comp_face_value_usd,
  pd.pos_cogs_usd - pd.comp_cost_usd AS non_comp_cogs_usd,
  coalesce(ll.redemptions, 0) AS redemptions, coalesce(ll.points_earned, 0) AS points_earned,
  coalesce(ll.points_redeemed, 0) AS points_redeemed, coalesce(fb.comp_tickets, 0) AS comp_tickets,
  coalesce(fb.carded_cash_tickets, 0) AS carded_cash_tickets, coalesce(fb.carded_cash_usd, 0) AS carded_cash_usd,
  coalesce(fb.uncarded_tickets, 0) AS uncarded_tickets, coalesce(fb.uncarded_cash_usd, 0) AS uncarded_cash_usd,
  pd.total_zone_entries_observed, pd.counter_coverage_share, pd.machine_supplied_costs_usd,
  pd.contribution_after_supplied_costs_usd, pd.tech_labor_cost_usd, pd.incidents_reported
FROM {S}.property_day pd
JOIN {S}.calendar_day cal ON cal.gaming_date = pd.gaming_date
CROSS JOIN closure c
LEFT JOIN md ON md.gaming_date = pd.gaming_date
LEFT JOIN ll ON ll.gaming_date = pd.gaming_date
LEFT JOIN fb ON fb.gaming_date = pd.gaming_date
LEFT JOIN dd ON CAST(dd.gaming_date AS DATE) = pd.gaming_date""",
      ["silver.property_day", "silver.calendar_day", "silver.loyalty_ledger", "silver.fnb_transactions",
       "gold.gold_machine_daily", "outputs/downtime-daily.csv", "silver.floor_walkways"], wave=2)

table("gold_downtime_tickets", "Q3", "operations ticket", ["operation_id"], SCOPE_FILTERABLE, f"""
WITH t AS (SELECT * FROM {src('downtime-tickets.csv')}),
zones AS (SELECT zone_id, name AS zone_name FROM {S}.floor_locations WHERE node_type = 'zone')
SELECT t.operation_id, CAST(t.gaming_date AS DATE) AS opened_gaming_date, date_format(CAST(t.gaming_date AS DATE), 'yyyy-MM') AS month,
  t.machine_id, t.location_id, md.bank_id, t.zone_id, z.zone_name, md.config_id, t.manufacturer, t.event_type AS cause,
  CAST(t.opened_at_local AS TIMESTAMP_NTZ) AS opened_at_local, t.shift,
  CAST(t.total_hours AS DOUBLE) AS total_hours, CAST(t.waiting_hours AS DOUBLE) AS waiting_hours,
  CAST(t.work_hours AS DOUBLE) AS work_hours, CAST(t.record_lag_minutes AS DOUBLE) AS record_lag_minutes,
  CAST(t.direct_cost_usd AS DOUBLE) AS direct_cost_usd,
  CAST(t.lost_coin_usd AS DOUBLE) AS est_gross_lost_coin_in_usd, CAST(t.lost_theo_usd AS DOUBLE) AS est_gross_lost_theo_usd,
  CAST(t.planned_headcount AS DOUBLE) AS planned_headcount, CAST(t.actual_headcount AS DOUBLE) AS actual_headcount,
  CAST(t.tech_shortfall AS DOUBLE) AS tech_shortfall, CAST(t.queue_waiting AS DOUBLE) AS queue_waiting,
  CAST(t.queue_in_work AS DOUBLE) AS queue_in_work, CAST(t.days_since_previous_fix AS DOUBLE) AS days_since_previous_fix,
  CAST(t.repeat_within_window AS BOOLEAN) AS repeat_within_window
FROM t
LEFT JOIN zones z ON z.zone_id = t.zone_id
LEFT JOIN {G}.gold_machine_daily md ON md.machine_id = t.machine_id AND md.gaming_date = CAST(t.gaming_date AS DATE)""",
      ["outputs/downtime-tickets.csv", "silver.floor_locations", "gold.gold_machine_daily"], wave=2)

SEGMENT_MEASURES = ["players_seen", "players_with_play", "players_ahead", "coin_in_usd", "payout_usd", "net_usd",
                    "house_win_usd", "theo_usd", "luck_residual_usd", "player_losses_usd", "player_winnings_usd",
                    "observed_visits", "observed_visits_players_with_play", "sessions", "play_hours",
                    "comps_redeemed", "comp_cost_usd",
                    "comp_face_value_usd", "points_earned", "points_redeemed", "linked_pos_cash_usd"]
UNIT_COLUMNS = ("coin_in_usd, payout_usd, net_usd, theo_usd, luck_residual_usd, observed_visits, sessions, play_hours, "
                "comps_redeemed, comp_cost_usd, comp_face_value_usd, points_earned, points_redeemed, linked_pos_cash_usd")
table("gold_player_segments", "Q4, Q5", "period (month or 6-month window) x guest dimension x segment",
      ["period_month", "dimension", "segment"], SCOPE_SEGMENTS, f"""
WITH base AS (
  SELECT pp.player_id, pp.period_month, pl.opening_tier, pl.home_region, coalesce(pl.age_band, 'unknown') AS age_band,
    CASE WHEN pl.enrolled_at_utc >= TIMESTAMP_NTZ'2025-01-01 14:00:00' THEN 'enrolled jan-jun 2025'
         ELSE 'enrolled before 2025' END AS enrollment_cohort,
    pp.tracked_coin_in_usd AS coin_in_usd, pp.tracked_payout_usd AS payout_usd, pp.tracked_net_usd AS net_usd,
    pp.theo_usd, pp.luck_residual_usd, pp.observed_visits, pp.sessions, pp.carded_play_hours AS play_hours,
    pp.comps_redeemed, pp.comp_cost_usd, pp.comp_face_value_usd, pp.points_earned, pp.points_redeemed,
    pp.linked_pos_cash_usd
  FROM {S}.player_period pp JOIN {S}.players pl ON pl.player_id = pp.player_id),
units AS (
  SELECT period_month, player_id, opening_tier, home_region, age_band, enrollment_cohort, {UNIT_COLUMNS} FROM base
  UNION ALL
  SELECT 'ALL', player_id, opening_tier, home_region, age_band, enrollment_cohort,
    SUM(coin_in_usd), SUM(payout_usd), SUM(net_usd), SUM(theo_usd), SUM(luck_residual_usd), SUM(observed_visits),
    SUM(sessions), SUM(play_hours), SUM(comps_redeemed), SUM(comp_cost_usd), SUM(comp_face_value_usd),
    SUM(points_earned), SUM(points_redeemed), SUM(linked_pos_cash_usd)
  FROM base GROUP BY player_id, opening_tier, home_region, age_band, enrollment_cohort),
cells AS (
  SELECT period_month,
    CASE WHEN grouping(opening_tier) = 0 THEN 'opening_tier' WHEN grouping(home_region) = 0 THEN 'home_region'
         WHEN grouping(age_band) = 0 THEN 'age_band' WHEN grouping(enrollment_cohort) = 0 THEN 'enrollment_cohort'
         ELSE 'all' END AS dimension,
    coalesce(opening_tier, home_region, age_band, enrollment_cohort, 'All') AS segment,
    COUNT(*) AS players_seen, COUNT_IF(coin_in_usd > 0) AS players_with_play,
    COUNT_IF(coin_in_usd > 0 AND net_usd > 0) AS players_ahead,
    SUM(coin_in_usd) AS coin_in_usd, SUM(payout_usd) AS payout_usd, SUM(net_usd) AS net_usd, -SUM(net_usd) AS house_win_usd,
    SUM(theo_usd) AS theo_usd, SUM(luck_residual_usd) AS luck_residual_usd,
    SUM(GREATEST(-net_usd, 0)) AS player_losses_usd, SUM(GREATEST(net_usd, 0)) AS player_winnings_usd,
    SUM(observed_visits) AS observed_visits,
    SUM(CASE WHEN coin_in_usd > 0 THEN observed_visits ELSE 0 END) AS observed_visits_players_with_play,
    SUM(sessions) AS sessions, SUM(play_hours) AS play_hours,
    SUM(comps_redeemed) AS comps_redeemed, SUM(comp_cost_usd) AS comp_cost_usd,
    SUM(comp_face_value_usd) AS comp_face_value_usd, SUM(points_earned) AS points_earned,
    SUM(points_redeemed) AS points_redeemed, SUM(linked_pos_cash_usd) AS linked_pos_cash_usd
  FROM units
  GROUP BY GROUPING SETS ((period_month), (period_month, opening_tier), (period_month, home_region),
                          (period_month, age_band), (period_month, enrollment_cohort))),
flagged AS (
  SELECT *, (players_seen < {MIN_CELL} OR (players_with_play > 0 AND players_with_play < {MIN_CELL})) AS primary_suppressed
  FROM cells),
ranked AS (
  SELECT *, SUM(CAST(primary_suppressed AS INT)) OVER (PARTITION BY period_month, dimension) AS n_primary,
    ROW_NUMBER() OVER (PARTITION BY period_month, dimension, primary_suppressed ORDER BY players_seen, segment) AS size_rank
  FROM flagged),
published AS (
  SELECT *, primary_suppressed OR (NOT primary_suppressed AND n_primary = 1 AND size_rank = 1 AND dimension <> 'all')
    AS suppressed FROM ranked)
SELECT period_month, CASE WHEN period_month = 'ALL' THEN 'window: 6 months, distinct players' ELSE 'month' END AS period_type,
  dimension, segment, suppressed,
  CASE WHEN primary_suppressed THEN 'fewer than {MIN_CELL} players'
       WHEN suppressed THEN 'secondary: prevents recovering a suppressed cell by subtraction' END AS suppression_reason,
  {", ".join(f"CASE WHEN suppressed THEN NULL ELSE {m} END AS {m}" for m in SEGMENT_MEASURES)},
  false AS grouping_depends_on_outcomes,
  {lit(SCOPE_SEGMENTS)} AS scope
FROM published""", ["silver.player_period", "silver.players"])

table("gold_forecast_daily", "forecast", "gaming date", ["gaming_date"], SCOPE_FORECAST, f"""
WITH f AS (SELECT * FROM {src('forecast-daily.csv')})
SELECT CAST(gaming_date AS DATE) AS gaming_date, weekday AS local_weekday,
  CASE WHEN period = 'test' THEN 'holdout (frozen out-of-sample forecast)' ELSE 'training (in-sample fit)' END AS sample,
  CASE WHEN period = 'test' THEN DATE'2025-04-30' END AS forecast_origin, CAST(horizon_days AS INT) AS horizon_days,
  CAST(actual AS DOUBLE) AS actual_coin_in_usd, CAST(constant_mean AS DOUBLE) AS forecast_constant_mean_usd,
  CAST(weekday_mean AS DOUBLE) AS forecast_weekday_mean_usd, CAST(ridge AS DOUBLE) AS forecast_ridge_usd,
  CAST(ridge_low AS DOUBLE) AS forecast_ridge_low_usd, CAST(ridge_high AS DOUBLE) AS forecast_ridge_high_usd,
  CAST(in_interval AS BOOLEAN) AS actual_in_ridge_interval,
  abs(CAST(actual AS DOUBLE) - CAST(constant_mean AS DOUBLE)) AS abs_error_constant_mean_usd,
  abs(CAST(actual AS DOUBLE) - CAST(weekday_mean AS DOUBLE)) AS abs_error_weekday_mean_usd,
  abs(CAST(actual AS DOUBLE) - CAST(ridge AS DOUBLE)) AS abs_error_ridge_usd,
  CAST(events_active AS STRING) AS events_active, CAST(federal_holiday AS STRING) AS federal_holiday, note,
  {lit(SCOPE_FORECAST)} AS scope
FROM f""", ["outputs/forecast-daily.csv"])

table("gold_model_validation", "forecast", "validation section x model x period x metric",
      ["section", "model", "period", "metric"], SCOPE_FORECAST, f"""
WITH perf AS (SELECT * FROM {src('forecast-performance.csv')}),
cmp AS (SELECT * FROM {src('forecast-model-comparison.csv')}),
summary AS (SELECT * FROM {src('forecast-validation-summary.csv')}),
folds AS (SELECT * FROM {src('forecast-validation-folds.csv')}),
decision AS (SELECT * FROM {src('forecast-decision.csv')}),
daily AS (SELECT * FROM {src('forecast-daily.csv')} WHERE period = 'test'),
selected AS (SELECT candidate FROM summary WHERE rank = 1),
test_mae AS (SELECT model, CAST(mae AS DOUBLE) AS mae FROM perf WHERE period LIKE 'test%'),
best AS (SELECT model, mae FROM test_mae ORDER BY mae LIMIT 1),
body AS (
  SELECT CASE WHEN period LIKE 'test%' THEN 'holdout test (may-jun, frozen at 2025-04-30)'
              WHEN period LIKE 'train%' THEN 'in-sample fit (jan-apr; not validation)'
              ELSE 'chronological validation (expanding-window folds feb-apr)' END AS section,
    model, period, CAST(days AS INT) AS days, metric, value, ci_low, ci_high,
    CASE WHEN metric = 'mae' AND ci_low IS NOT NULL THEN '95% ci from 4000 bootstrap draws of the 9 test week blocks' END AS note
  FROM (SELECT model, period, days, stack(4,
      'mae', CAST(mae AS DOUBLE), CAST(mae_ci_low AS DOUBLE), CAST(mae_ci_high AS DOUBLE),
      'rmse', CAST(rmse AS DOUBLE), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
      'mape', CAST(mape AS DOUBLE), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE),
      'mean_error', CAST(mean_error AS DOUBLE), CAST(NULL AS DOUBLE), CAST(NULL AS DOUBLE)) AS (metric, value, ci_low, ci_high)
    FROM perf)
  WHERE value IS NOT NULL
  UNION ALL
  SELECT 'paired holdout comparison (week-block bootstrap)', comparison, 'test may-jun', 61,
    'mae difference (usd; negative = first model more accurate)', CAST(mae_difference AS DOUBLE),
    CAST(ci_low AS DOUBLE), CAST(ci_high AS DOUBLE),
    concat('share of bootstrap draws below zero: ', format_number(CAST(share_of_draws_below_zero AS DOUBLE), 3))
  FROM cmp
  UNION ALL
  SELECT 'model selection (mean mae over validation folds)', candidate, 'validation feb-apr', NULL, 'mean fold mae',
    CAST(mean_mae AS DOUBLE), NULL, NULL,
    concat('rank ', CAST(rank AS STRING), '; holiday=', coalesce(CAST(holiday AS STRING), '-'), ', events=',
           coalesce(CAST(events AS STRING), '-'), ', closure=', coalesce(CAST(closure AS STRING), '-'), ', trend=',
           coalesce(CAST(trend AS STRING), '-'), ', log_target=', coalesce(CAST(log_target AS STRING), '-'), ', alpha=',
           coalesce(CAST(alpha AS STRING), '-'))
  FROM summary WHERE rank <= 3 OR candidate IN ('constant mean', 'weekday mean')
  UNION ALL
  SELECT 'validation folds (selected model and baselines)', candidate, CAST(fold AS STRING), CAST(train_days AS INT),
    'fold mae', CAST(mae AS DOUBLE), NULL, NULL, concat('origin ', CAST(origin AS STRING))
  FROM folds WHERE candidate IN ('constant mean', 'weekday mean') OR candidate IN (SELECT candidate FROM selected)
  UNION ALL
  SELECT 'interval calibration (holdout)', 'ridge (selected)', 'test may-jun', COUNT(*),
    'share of days inside the nominal 90% interval', AVG(CAST(CAST(in_interval AS BOOLEAN) AS INT)), NULL, NULL,
    concat(CAST(SUM(CAST(CAST(in_interval AS BOOLEAN) AS INT)) AS STRING), ' of ', CAST(COUNT(*) AS STRING),
           ' holdout days inside the interval vs 90% nominal; the interval is too narrow and understates uncertainty')
  FROM daily
  UNION ALL
  SELECT 'decision value (holdout)', rule, 'test may-jun', CAST(coalesce(days, weeks) AS INT),
    'mean actual coin-in on chosen work days (usd)', CAST(mean_actual_coin_in_usd AS DOUBLE), NULL, NULL,
    concat(decision, CASE WHEN coin_in_vs_random_day IS NOT NULL
      THEN concat('; vs random day ', format_number(CAST(coin_in_vs_random_day AS DOUBLE) * 100, 1), '%') ELSE '' END,
      CASE WHEN coin_in_vs_average_day IS NOT NULL
      THEN concat('; vs average day ', format_number(CAST(coin_in_vs_average_day AS DOUBLE) * 100, 1), '%') ELSE '' END)
  FROM decision
  UNION ALL
  SELECT 'disclosure', b.model, 'test may-jun', 61, 'lowest holdout mae (usd)', b.mae, NULL, NULL,
    concat('on the 61-day holdout the ', b.model, ' baseline had the lowest mae ($', format_number(b.mae, 0),
      ') vs ridge ($', format_number(r.mae, 0), '); ridge minus weekday mean = $',
      format_number(CAST(c.mae_difference AS DOUBLE), 0), ' (95% ci $', format_number(CAST(c.ci_low AS DOUBLE), 0),
      ' to $', format_number(CAST(c.ci_high AS DOUBLE), 0), ')',
      CASE WHEN CAST(c.ci_low AS DOUBLE) < 0 AND CAST(c.ci_high AS DOUBLE) > 0
        THEN '; the interval includes zero, so the two are not clearly different, and the simpler baseline is not beaten'
        ELSE '' END)
  FROM best b CROSS JOIN (SELECT mae FROM test_mae WHERE model LIKE 'ridge%') r
  CROSS JOIN (SELECT * FROM cmp WHERE comparison = 'ridge - weekday mean') c)
SELECT *, {lit(SCOPE_FORECAST)} AS scope FROM body""",
      ["outputs/forecast-performance.csv", "outputs/forecast-model-comparison.csv",
       "outputs/forecast-validation-summary.csv", "outputs/forecast-validation-folds.csv",
       "outputs/forecast-decision.csv", "outputs/forecast-daily.csv"])

table("gold_investment_plan", "Q6", "funded action x target", ["action_type_id", "target"], SCOPE_PLAN, f"""
WITH t AS (SELECT * FROM {src('investment-targets.csv')}),
p AS (SELECT * FROM {src('investment-recommended-plan.csv')}),
mp AS (SELECT * FROM {src('investment-move-plan.csv')}),
mm AS (SELECT * FROM {src('investment-measurement-plan.csv')}),
sens AS (SELECT action_type_id,
  MAX(CASE WHEN scenario = 'low' THEN assumptions END) AS low_assumptions,
  MAX(CASE WHEN scenario = 'central' THEN assumptions END) AS central_assumptions,
  MAX(CASE WHEN scenario = 'high' THEN assumptions END) AS high_assumptions,
  MAX(CASE WHEN scenario = 'in-budget financial benefit' THEN assumptions END) AS in_budget_note
  FROM {src('investment-sensitivity.csv')} GROUP BY action_type_id),
body AS (
  SELECT t.action_type_id, p.action, t.target, t.detail, mp.from_location, mp.to_location, mp.to_zone,
    CAST(t.order_date AS DATE) AS order_date, CAST(t.earliest_effective AS DATE) AS earliest_effective_date,
    CAST(t.scheduled_date AS DATE) AS scheduled_date, CAST(t.downtime_minutes AS INT) AS downtime_minutes,
    CAST(p.effect_days AS INT) AS effect_days, CAST(p.quantity AS INT) AS action_quantity,
    CAST(p.total_cost_usd AS DOUBLE) AS action_total_cost_usd,
    CASE WHEN t.action_type_id = 'A-MOVE-MACHINE' THEN CAST(mp.unit_cost_usd AS DOUBLE) ELSE CAST(p.total_cost_usd AS DOUBLE) END AS target_cost_usd,
    CASE WHEN t.action_type_id = 'A-MOVE-MACHINE' THEN CAST(mp.downtime_cost_usd AS DOUBLE) ELSE CAST(p.downtime_cost_usd AS DOUBLE) END AS target_downtime_cost_usd,
    CASE WHEN t.action_type_id = 'A-MOVE-MACHINE' THEN CAST(mp.net_low_usd AS DOUBLE) ELSE CAST(p.benefit_low_usd AS DOUBLE) END AS benefit_low_usd,
    CASE WHEN t.action_type_id = 'A-MOVE-MACHINE' THEN CAST(mp.net_central_usd AS DOUBLE) ELSE CAST(p.benefit_central_usd AS DOUBLE) END AS benefit_central_usd,
    CASE WHEN t.action_type_id = 'A-MOVE-MACHINE' THEN CAST(mp.net_high_usd AS DOUBLE) ELSE CAST(p.benefit_high_usd AS DOUBLE) END AS benefit_high_usd,
    p.funding_basis, p.evidence_type, p.basis, p.notes, CAST(p.break_even AS DOUBLE) AS action_break_even,
    p.break_even_unit AS action_break_even_unit, CAST(mp.break_even_cannibalization AS DOUBLE) AS target_break_even_cannibalization,
    CAST(mp.machine_factor AS DOUBLE) AS machine_factor, CAST(mp.config_factor AS DOUBLE) AS game_appeal_factor,
    CAST(mp.config_factor_min_zone AS DOUBLE) AS game_appeal_min_zone, CAST(mp.config_factor_max_zone AS DOUBLE) AS game_appeal_max_zone,
    CAST(p.evidence_supports_funding AS BOOLEAN) AS evidence_supports_funding,
    CAST(p.robust_or_verifiable AS BOOLEAN) AS robust_or_verifiable,
    sens.low_assumptions, sens.central_assumptions, sens.high_assumptions, sens.in_budget_note,
    mm.success_metric, mm.control, mm.observation_period, mm.confounders, mm.continue_reverse_expand AS decision_rule
  FROM t
  JOIN p ON p.action_type_id = t.action_type_id
  LEFT JOIN mp ON t.action_type_id = 'A-MOVE-MACHINE' AND mp.machine_id = t.target
  LEFT JOIN mm ON mm.action_type_id = t.action_type_id
  LEFT JOIN sens ON sens.action_type_id = t.action_type_id)
SELECT *,
  benefit_low_usd - target_cost_usd - target_downtime_cost_usd AS net_low_usd,
  benefit_central_usd - target_cost_usd - target_downtime_cost_usd AS net_central_usd,
  benefit_high_usd - target_cost_usd - target_downtime_cost_usd AS net_high_usd,
  {BUDGET_USD} AS budget_usd, SUM(target_cost_usd) OVER () AS plan_total_cost_usd,
  {BUDGET_USD} - SUM(target_cost_usd) OVER () AS unallocated_usd,
  {lit(SCOPE_PLAN)} AS scope
FROM body""", ["outputs/investment-targets.csv", "outputs/investment-recommended-plan.csv",
               "outputs/investment-move-plan.csv", "outputs/investment-measurement-plan.csv",
               "outputs/investment-sensitivity.csv"])

SCENARIO_COLUMNS = ("question, scenario_group, item, scenario, kind, value_measure, cost_usd, value_low_usd, "
                    "value_central_usd, value_high_usd, break_even, break_even_unit, feasible, in_recommended_plan, "
                    "assumptions, source_file")
table("gold_scenarios", "Q2, Q3, Q6", "scenario row", ["scenario_id"], SCOPE_PLAN, f"""
WITH body AS (
  SELECT 'Q6' AS question, 'budget alternatives (net theo over the plan horizon)' AS scenario_group, plan AS item,
    actions AS scenario,
    CASE WHEN CAST(feasible AS BOOLEAN) THEN 'decision scenario' ELSE 'infeasible: exceeds the $100,000 budget' END AS kind,
    'net expected win (theo) after cost and downtime' AS value_measure, CAST(cost_usd AS DOUBLE) AS cost_usd,
    CAST(net_low_usd AS DOUBLE) AS value_low_usd, CAST(net_central_usd AS DOUBLE) AS value_central_usd,
    CAST(net_high_usd AS DOUBLE) AS value_high_usd, CAST(NULL AS DOUBLE) AS break_even, CAST(NULL AS STRING) AS break_even_unit,
    CAST(feasible AS BOOLEAN) AS feasible, plan = 'recommended' AS in_recommended_plan, notes AS assumptions,
    'outputs/investment-alternatives.csv' AS source_file
  FROM {src('investment-alternatives.csv')}
  UNION ALL
  SELECT 'Q6', 'action sensitivity (gross benefit, theo)', action_type_id, scenario, kind,
    'gross incremental expected win (theo) before cost', NULL, NULL, CAST(benefit_usd AS DOUBLE), NULL, NULL, NULL,
    NULL, kind LIKE 'in plan net%' AND action_type_id IN ('A-INSPECT', 'A-MOVE-MACHINE'), assumptions,
    'outputs/investment-sensitivity.csv'
  FROM {src('investment-sensitivity.csv')}
  UNION ALL
  SELECT 'Q3', 'maintenance and staffing (not funded)', concat(action_type_id, ' ', target), scenario,
    'decision scenario', CASE WHEN net_theo_benefit_usd IS NULL THEN 'break-even fault reduction'
      ELSE 'net expected win (theo) over effect days, before action cost' END,
    CAST(cost_usd AS DOUBLE), CAST(net_benefit_low_usd AS DOUBLE), CAST(net_theo_benefit_usd AS DOUBLE),
    CAST(net_benefit_high_usd AS DOUBLE), CAST(break_even_fault_reduction AS DOUBLE),
    CASE WHEN break_even_fault_reduction IS NOT NULL THEN 'share of expected faults that must be prevented to break even' END,
    true, false,
    concat('effect days ', CAST(effect_days AS STRING),
      CASE WHEN benefit_to_cost IS NOT NULL THEN concat('; benefit to cost ', format_number(CAST(benefit_to_cost AS DOUBLE), 3)) ELSE '' END,
      CASE WHEN expected_faults_in_effect_days IS NOT NULL
        THEN concat('; expected faults in effect days ', format_number(CAST(expected_faults_in_effect_days AS DOUBLE), 1)) ELSE '' END),
    'outputs/downtime-action-economics.csv'
  FROM {src('downtime-action-economics.csv')}
  UNION ALL
  SELECT 'Q3', 'lost play from downtime (counterfactual estimate)', measure, method, 'counterfactual estimate',
    'usd over 2025-01-01 to 2025-06-30', NULL, CAST(ci_low_usd AS DOUBLE), CAST(estimate_usd AS DOUBLE),
    CAST(ci_high_usd AS DOUBLE), NULL, NULL, NULL, false, basis, 'outputs/downtime-lost-play.csv'
  FROM {src('downtime-lost-play.csv')}
  UNION ALL
  SELECT 'Q2', 'back forty walkway closure (counterfactual estimate)', concat(`zone`, ' closure, ', CAST(closure_days AS STRING), ' days'),
    stack_label, 'counterfactual estimate', 'usd over the closure days', NULL, lo, mid, hi, NULL, NULL, NULL, false,
    'play missing if back forty had kept its pre-closure gap to the other zones (difference-in-differences); an estimate',
    'outputs/demand-closure-impact.csv'
  FROM (SELECT `zone`, closure_days, stack(2,
      'lost coin-in', CAST(lost_coin_in_ci_low_usd AS DOUBLE), CAST(lost_coin_in_usd AS DOUBLE), CAST(lost_coin_in_ci_high_usd AS DOUBLE),
      'lost theo', CAST(lost_theo_ci_low_usd AS DOUBLE), CAST(lost_theo_usd AS DOUBLE), CAST(lost_theo_ci_high_usd AS DOUBLE))
      AS (stack_label, lo, mid, hi)
    FROM {src('demand-closure-impact.csv')}))
SELECT ROW_NUMBER() OVER (ORDER BY question, scenario_group, item, scenario) AS scenario_id, {SCENARIO_COLUMNS},
  CASE WHEN question = 'Q6' THEN {lit(SCOPE_PLAN)} ELSE {lit(SCOPE_WINDOW)} END AS scope
FROM body""", ["outputs/investment-alternatives.csv", "outputs/investment-sensitivity.csv",
               "outputs/downtime-action-economics.csv", "outputs/downtime-lost-play.csv",
               "outputs/demand-closure-impact.csv"])


def ev(source, question, topic, metric, estimate, unit, evidence_class, causal, subgroup="NULL", comparison="NULL",
       ci_low="NULL", ci_high="NULL", notes="NULL", where="", frm=None):
    #every argument except source, question and topic is a sql expression
    return (f"SELECT {lit(question)} AS question, {lit(topic)} AS topic, CAST({metric} AS STRING) AS metric, "
            f"CAST({subgroup} AS STRING) AS subgroup, CAST({comparison} AS STRING) AS comparison, "
            f"CAST({estimate} AS DOUBLE) AS estimate, CAST({ci_low} AS DOUBLE) AS ci_low, CAST({ci_high} AS DOUBLE) AS ci_high, "
            f"CAST({unit} AS STRING) AS unit, CAST({evidence_class} AS STRING) AS evidence_class, "
            f"CAST({causal} AS STRING) AS causal_interpretation, CAST({notes} AS STRING) AS notes, "
            f"{lit('outputs/' + source)} AS source_file FROM {frm or src(source)} {where}")


NOT_CAUSAL = lit("not causal: association only")
M1158_HIT_NOTE = (
    "format_string('counts how often a spin paid anything: %,d hits in %,d spins (%.2f%% vs published %.2f%%). a hit is a "
    "low-variance yes/no outcome, so with this many spins the standard error is about %.3f points and the gap gives z = %.1f. "
    "it ignores payout size. a normal p-value is not meaningful at this size; read it as far outside the published hit "
    "probability in every month. assumes independent spins and that the published probability applies.', "
    "CAST(payout_positive_spins AS BIGINT), CAST(paid_spins AS BIGINT), CAST(hit_rate AS DOUBLE) * 100, "
    "CAST(hit_probability AS DOUBLE) * 100, "
    "sqrt(CAST(hit_probability AS DOUBLE) * (1 - CAST(hit_probability AS DOUBLE)) / CAST(paid_spins AS DOUBLE)) * 100, "
    "CAST(hit_z AS DOUBLE))")
M1158_LUCK_NOTE = (
    "format_string('compares money: house win $%,.0f vs theo $%,.0f (residual $%,.0f). payout sizes vary widely (payout "
    "variance %.2f bet-squared per spin), so the residual has sd $%,.0f and z = %.2f (bonferroni p = %.4f across 310 "
    "machines). same direction as the hit-rate result: paying out more often lowers hold.', "
    "CAST(house_win_usd AS DOUBLE), CAST(theo_usd AS DOUBLE), CAST(luck_residual_usd AS DOUBLE), "
    "CAST(payout_multiplier_variance AS DOUBLE), CAST(luck_sd_usd AS DOUBLE), CAST(luck_z AS DOUBLE), "
    "CAST(luck_p_bonferroni AS DOUBLE))")
M1158_RECONCILE_NOTE = lit(
    "the two statistics measure different outcomes with very different noise and are consistent, not contradictory: "
    "hit z tests how often spins pay (low noise per spin, so a 7-point gap is enormous in z terms); luck z tests the money "
    "result (high noise because payout sizes vary). both point the same way. both are recomputed in databricks from silver "
    "meters and match the q1 output. supported claim: m-1158's payout pattern does not match its published configuration "
    "on two related measures; this is a lead to inspect, not evidence of a malfunction or wrongdoing.")

EVIDENCE_PARTS = [
    #q1 m-1158
    ev("machine-performance.csv", "Q1", "M-1158 payout pattern", "metric", "estimate", "unit", lit("statistical test"),
       lit("descriptive test against published game math; does not identify a cause"), subgroup="'M-1158'",
       comparison="comparison", notes="notes",
       frm=f"(SELECT stack(4, "
           f"'hit-rate z (binomial test vs published hit probability)', CAST(hit_z AS DOUBLE), 'z', 'published hit probability', {M1158_HIT_NOTE}, "
           f"'luck z (house win vs theo under published payout variance)', CAST(luck_z AS DOUBLE), 'z', 'theo (expected win)', {M1158_LUCK_NOTE}, "
           f"'luck p-value, bonferroni over 310 machines', CAST(luck_p_bonferroni AS DOUBLE), 'probability', 'published game math', 'two-sided normal p x 310, capped at 1', "
           f"'reconciliation: hit z 124 vs luck z -4.57', CAST(hit_z AS DOUBLE), 'z', 'luck z', {M1158_RECONCILE_NOTE}) "
           f"AS (metric, estimate, unit, comparison, notes) FROM {src('machine-performance.csv')} WHERE machine_id = 'M-1158')"),
    ev("machine-performance.csv", "Q1", "M-1158 payout pattern", "metric", "estimate", "unit", lit("observed"),
       lit("observed"), subgroup="'M-1158'", comparison="comparison",
       frm=f"(SELECT stack(4, 'observed hold', CAST(observed_hold AS DOUBLE), 'fraction', 'configured hold', "
           f"'configured (theo) hold', CAST(theo_hold AS DOUBLE), 'fraction', CAST(NULL AS STRING), "
           f"'hit rate', CAST(hit_rate AS DOUBLE), 'fraction', 'published hit probability', "
           f"'published hit probability', CAST(hit_probability AS DOUBLE), 'fraction', CAST(NULL AS STRING)) "
           f"AS (metric, estimate, unit, comparison) FROM {src('machine-performance.csv')} WHERE machine_id = 'M-1158')"),
    ev("m-1158-monthly.csv", "Q1", "M-1158 payout pattern by month", "metric", "estimate", "'z'", lit("statistical test"),
       lit("descriptive test against published game math; does not identify a cause"), subgroup="month_label",
       comparison="'published game math'",
       frm=f"(SELECT month_label, stack(2, 'hit-rate z', CAST(hit_z AS DOUBLE), 'luck z', CAST(luck_z AS DOUBLE)) "
           f"AS (metric, estimate) FROM {src('m-1158-monthly.csv')})"),
    ev("config-hit-rates.csv", "Q1", "published hit probability is comparable", "'observed hit rate (all machines on config)'",
       "observed_hit_rate", "'fraction'", lit("observed"), lit("observed"), subgroup="config_id",
       comparison="concat('published ', CAST(published_hit_probability AS STRING))",
       notes="concat('machine range ', format_number(CAST(min_machine_hit_rate AS DOUBLE), 4), '-', format_number(CAST(max_machine_hit_rate AS DOUBLE), 4))"),
    #q2 closure, placebos, moves, drivers
    ev("demand-closure-did.csv", "Q2", "back forty walkway closure", "outcome", "estimate_pct", "'relative change'",
       "CASE WHEN contrast LIKE 'pre-trend%' THEN 'pre-trend check' ELSE 'quasi-experimental estimate (difference-in-differences)' END",
       "CASE WHEN contrast LIKE 'pre-trend%' THEN 'tests the parallel-trends assumption' ELSE 'causal only if back forty would have tracked the comparison absent the closure; see pre-trend and placebo rows' END",
       subgroup="sample", comparison="concat(comparison, ' | ', contrast)", ci_low="ci_low_pct", ci_high="ci_high_pct",
       notes="concat('bootstrap p ', CAST(bootstrap_p AS STRING), '; days before ', CAST(days_before AS STRING), ', after ', CAST(days_after AS STRING))"),
    ev("demand-closure-placebo.csv", "Q2", "back forty walkway closure", "outcome", "estimate_pct", "'relative change'",
       "CASE WHEN CAST(is_actual AS BOOLEAN) THEN 'quasi-experimental estimate (difference-in-differences)' ELSE 'placebo test' END",
       "CASE WHEN CAST(is_actual AS BOOLEAN) THEN 'the actual closure estimate' ELSE 'should be near zero if the design is sound' END",
       subgroup="concat('placebo ', placebo, ': ', treated)", comparison="concat('window from ', CAST(window_start AS STRING))"),
    ev("demand-machine-moves.csv", "Q2", "machine relocations", "metric", "estimate", "'relative change'",
       lit("natural experiment (difference-in-differences vs same-config machines)"),
       lit("causal for these 4 moves only if peers are a valid control; small sample"), subgroup="machine_id",
       comparison="concat(origin_zone, ' -> ', destination_zone, ' on ', CAST(move_gaming_date AS STRING))",
       ci_low="lo", ci_high="hi", notes="p_note",
       frm=f"(SELECT machine_id, origin_zone, destination_zone, move_gaming_date, stack(2, "
           f"'coin-in per hour vs peers', CAST(did_coin_in_pct AS DOUBLE), CAST(did_coin_in_ci_low_pct AS DOUBLE), CAST(did_coin_in_ci_high_pct AS DOUBLE), concat('bootstrap p ', CAST(did_coin_in_bootstrap_p AS STRING)), "
           f"'occupancy vs peers', CAST(did_occupancy_pct AS DOUBLE), CAST(did_occupancy_ci_low_pct AS DOUBLE), CAST(did_occupancy_ci_high_pct AS DOUBLE), CAST(NULL AS STRING)) "
           f"AS (metric, estimate, lo, hi, p_note) FROM {src('demand-machine-moves.csv')})"),
    ev("demand-drivers.csv", "Q2", "property demand drivers", "term", "effect_pct", "'relative change'",
       lit("association (regression, newey-west errors)"), NOT_CAUSAL, comparison="'log daily coin-in model'",
       ci_low="ci_low_pct", ci_high="ci_high_pct", notes="concat('days flagged ', coalesce(CAST(days_flagged AS STRING), '-'))",
       where="WHERE term <> 'intercept'"),
    #q3
    ev("downtime-delay-model.csv", "Q3", "technician waiting time", "term", "coefficient_hours", "'hours'",
       lit("association (ols, errors clustered by gaming date)"), NOT_CAUSAL, ci_low="ci_low", ci_high="ci_high",
       where="WHERE term <> 'intercept'"),
    ev("downtime-manufacturers.csv", "Q3", "fault rates by manufacturer", "'faults per 1,000 machine-days'",
       "faults_per_1000_machine_days", "'rate'", lit("observed (poisson ci)"), lit("descriptive"), subgroup="manufacturer",
       ci_low="ci_low", ci_high="ci_high"),
    ev("downtime-machine-age.csv", "Q3", "fault rates by machine age", "'faults per 1,000 machine-days'",
       "faults_per_1000_machine_days", "'rate'", lit("observed (poisson ci)"), lit("descriptive; age is confounded with manufacturer"),
       subgroup="age_band", ci_low="ci_low", ci_high="ci_high"),
    ev("downtime-model-diagnostics.csv", "Q3", "lost-play model", "metric", "estimate", "'model unit'",
       lit("model estimate (jackknife ci)"), lit("estimate under the expected-play model"), ci_low="ci_low", ci_high="ci_high"),
    #q4
    ev("players-gini.csv", "Q4", "concentration of carded play", "concat('gini of ', measure)", "gini", "'index 0-1'",
       lit("observed (player bootstrap ci)"), lit("descriptive"), ci_low="ci_low", ci_high="ci_high"),
    ev("players-concentration.csv", "Q4", "concentration of carded play", "concat('share of ', measure)", "share_of_total",
       "'fraction'", lit("observed (player bootstrap ci)"), lit("descriptive"),
       subgroup="concat('top ', CAST(CAST(top_share_of_players * 100 AS INT) AS STRING), '% ', ranked_by)",
       comparison="concat(CAST(players_in_group AS STRING), ' players')", ci_low="ci_low", ci_high="ci_high", notes="note"),
    ev("players-tier-hold.csv", "Q4", "hold by opening tier", "'observed hold'", "observed_hold", "'fraction'",
       lit("observed (bootstrap ci)"), lit("descriptive"), subgroup="opening_tier", ci_low="ci_low", ci_high="ci_high"),
    ev("players-luck-calibration.csv", "Q4", "player luck calibration", "metric", "estimate", "unit", lit("statistical test"),
       lit("descriptive check of the luck model"),
       frm=f"(SELECT stack(3, 'player luck z sd (about 1 if calibrated)', CAST(z_sd AS DOUBLE), 'z', "
           f"'share of players with |z| > 2', CAST(share_abs_z_over_2 AS DOUBLE), 'fraction', "
           f"'carded total luck z', CAST(carded_total_luck_z AS DOUBLE), 'z') AS (metric, estimate, unit) "
           f"FROM {src('players-luck-calibration.csv')})"),
    #q5
    ev("loyalty-policy-change.csv", "Q5", "may 1 comp price change", "outcome", "change", "'relative change'",
       "CASE WHEN spec LIKE '%its%' THEN 'interrupted time series (newey-west)' ELSE 'before/after ratio (week-block bootstrap)' END",
       lit("before/after association; other changes in may-june are not ruled out"), subgroup="spec",
       ci_low="ci_low", ci_high="ci_high", notes="concat('controls: ', coalesce(controls, 'none'))"),
    ev("loyalty-local-comparison.csv", "Q5", "local vs non-local comps", "metric", "estimate", "unit",
       lit("observed (player bootstrap ci)"), lit("descriptive; local = pre-2025 members, so the two are confounded"),
       subgroup="scope", ci_low="lo", ci_high="hi",
       frm=f"(SELECT scope, stack(4, "
           f"'reinvestment ratio local vs non-local', CAST(reinvestment_ratio_local_vs_nonlocal AS DOUBLE), CAST(reinvestment_ratio_local_vs_nonlocal_ci_low AS DOUBLE), CAST(reinvestment_ratio_local_vs_nonlocal_ci_high AS DOUBLE), 'ratio', "
           f"'comp cost per visit ratio local vs non-local', CAST(comp_per_visit_ratio_local_vs_nonlocal AS DOUBLE), CAST(comp_per_visit_ratio_local_vs_nonlocal_ci_low AS DOUBLE), CAST(comp_per_visit_ratio_local_vs_nonlocal_ci_high AS DOUBLE), 'ratio', "
           f"'local share of comp cost', CAST(local_share_of_comp_cost AS DOUBLE), CAST(local_share_of_comp_cost_ci_low AS DOUBLE), CAST(local_share_of_comp_cost_ci_high AS DOUBLE), 'fraction', "
           f"'local share of carded theo', CAST(local_share_of_theo AS DOUBLE), CAST(local_share_of_theo_ci_low AS DOUBLE), CAST(local_share_of_theo_ci_high AS DOUBLE), 'fraction') "
           f"AS (metric, estimate, lo, hi, unit) FROM {src('loyalty-local-comparison.csv')})"),
    ev("loyalty-comp-concentration.csv", "Q5", "comp concentration", "metric", "estimate", "'fraction'", lit("observed"),
       lit("descriptive"), subgroup="concat('top ', CAST(CAST(top_share_of_redeemers * 100 AS INT) AS STRING), '% of redeemers')",
       frm=f"(SELECT top_share_of_redeemers, stack(2, 'share of comp cost', CAST(share_of_comp_cost AS DOUBLE), "
           f"'share of carded theo', CAST(share_of_carded_theo AS DOUBLE)) AS (metric, estimate) "
           f"FROM {src('loyalty-comp-concentration.csv')})"),
    #q6
    ev("investment-promotion-evidence.csv", "Q6", "promotion weekend evidence", "concat(event, ' (', `window`, ')')", "effect",
       "'relative change'", "evidence_kind", NOT_CAUSAL, comparison="concat(CAST(start_date AS STRING), ' to ', CAST(end_date AS STRING))",
       ci_low="ci_low", ci_high="ci_high", notes="concat(CAST(days AS STRING), ' days')"),
]
table("gold_evidence", "Q1-Q6", "statistical estimate or test", ["evidence_id"], SCOPE_WINDOW,
      "WITH body AS (" + "\nUNION ALL\n".join(EVIDENCE_PARTS) + ")\n"
      "SELECT ROW_NUMBER() OVER (ORDER BY question, topic, metric, subgroup, comparison, estimate) AS evidence_id, *, "
      f"{lit(SCOPE_WINDOW)} AS scope FROM body",
      sorted({"outputs/" + p.split("'outputs/")[1].split("'")[0] for p in EVIDENCE_PARTS}))

GOLD_TABLES = [t["name"] for t in TABLES]
LOG_TABLE = "gold_build_log"


#column definitions applied as column comments wherever the column exists
COLUMN_DOCS = {
    "coin_in_usd": "observed: total wagered incl. replayed winnings",
    "payout_usd": "observed: total paid back by the game",
    "house_win_usd": "observed actual revenue: coin-in minus payout; can be negative",
    "theo_usd": "expected (theoretical) win: coin-in x (1 - configured rtp); not actual revenue",
    "luck_residual_usd": "actual minus expected: house win minus theo (player tables: net plus theo)",
    "carded_coin_in_usd": "observed: loyalty-card (carded) share of coin-in",
    "carded_theo_usd": "expected win on carded play",
    "contribution_usd": "observed: house win minus supplied lease, service, power and repair costs",
    "scheduled_hours": "on-floor machine-hours in the gaming day (23 on the dst day)",
    "available_hours": "hours the machine could take play",
    "occupied_hours": "hours with play",
    "downtime_hours": "observed: scheduled minus available hours",
    "luck_variance_usd2": "additive variance of house win under published math; luck z = sum(luck_residual_usd) / sqrt(sum(luck_variance_usd2))",
    "expected_hit_spins": "spins x published hit probability; hit z = (sum(hit_spins) - sum(expected_hit_spins)) / sqrt(sum(hit_variance))",
    "hit_variance": "binomial variance of hits: spins x p x (1 - p)",
    "hit_spins": "observed: spins that paid anything",
    "est_gross_lost_coin_in_usd": "estimate (counterfactual): coin-in that did not happen during downtime; assumes no displacement",
    "est_gross_lost_theo_usd": "estimate (counterfactual): expected win lost to downtime; assumes no displacement",
    "comp_cost_usd": "actual economic cost of redeemed comps (linked f&b cogs); already inside pos_cogs_usd, count once",
    "comp_face_value_usd": "menu value of comped items; not revenue and not a cost",
    "pos_cogs_usd": "f&b cost of goods including comp cost",
    "non_comp_cogs_usd": "f&b cost of goods excluding comps (pos_cogs_usd - comp_cost_usd)",
    "closure_period": "pre / closure / post relative to the back forty walkway closure (2025-03-03 to 2025-04-06)",
    "demand_index": "model-based (in-sample zone+config baseline): play attracted vs peers; 1 = typical; fixed full-window scope",
    "actual_win_index": "model-based: actual win per available hour vs the same baseline; includes luck and payout deviations",
    "luck_z": "house win vs theo in sd units under published payout variance",
    "hit_z": "binomial z of hit count vs published hit probability",
    "forecast_ridge_usd": "forecast estimate (frozen 2025-04-30); in-sample fit on training rows",
    "forecast_weekday_mean_usd": "forecast estimate: jan-apr weekday mean baseline",
    "forecast_constant_mean_usd": "forecast estimate: jan-apr constant mean baseline",
    "actual_coin_in_usd": "observed whole-property coin-in",
    "value_low_usd": "scenario value: pessimistic evidence-consistent case",
    "value_central_usd": "scenario value: central case",
    "value_high_usd": "scenario value: optimistic case",
    "net_low_usd": "scenario: benefit minus cost and downtime, low case (theo)",
    "net_central_usd": "scenario: benefit minus cost and downtime, central case (theo)",
    "net_high_usd": "scenario: benefit minus cost and downtime, high case (theo)",
    "suppressed": f"true when the cell is hidden: fewer than {MIN_CELL} players or secondary suppression",
    "scope": "what the row covers and whether it responds to dashboard filters",
    "evidence_class": "observed, statistical test, association, quasi-experimental estimate, placebo test or model estimate",
    "causal_interpretation": "what causal reading the evidence supports, if any",
}


def create_sql(spec, build_id, built_at):
    properties = {"ante.layer": "gold", "ante.build_id": build_id, "ante.built_at": built_at,
                  "ante.question": spec["question"], "ante.grain": spec["grain"], "ante.scope": spec["scope"],
                  "ante.sources": ", ".join(spec["sources"]), "ante.contains_player_level": "false",
                  "ante.publishable": "true"}
    props = ", ".join(f"{lit(k)} = {lit(v)}" for k, v in properties.items())
    comment = f"{spec['question']}: {spec['grain']}; keys: {', '.join(spec['keys'])}"
    return f"CREATE OR REPLACE TABLE {G}.{spec['name']} COMMENT {lit(comment)} TBLPROPERTIES ({props}) AS {spec['sql']}"


#=== checks ===

CHECKS = []


def check(table_name, name, category, sql, expected=0, severity="error"):
    #sql returns one row with column observed (and optional detail); pass when observed == expected
    CHECKS.append({"table": table_name, "check": name, "category": category, "sql": sql, "expected": expected,
                   "severity": severity})


def mismatches(table_name, name, sql_from, condition, key, category="reconciliation", severity="error"):
    check(table_name, name, category,
          f"SELECT COUNT(*) AS observed, concat_ws('; ', slice(collect_list(CAST({key} AS STRING)), 1, 8)) AS detail "
          f"FROM {sql_from} WHERE {condition}", severity=severity)


def differs(a, b, tolerance=0.01):
    return f"((({a}) IS NULL) <> (({b}) IS NULL) OR abs(({a}) - ({b})) > {tolerance})"


def rel_differs(a, b, tolerance=1e-6):
    return f"((({a}) IS NULL) <> (({b}) IS NULL) OR abs(({a}) - ({b})) > {tolerance} * GREATEST(1, abs({b})))"


def add_checks(local_hashes, build_start):
    expected_rows = {"gold_machine_daily": f"(SELECT COUNT(*) FROM {S}.prepared_daily_view WHERE active)",
                     "gold_machine_scorecard": f"(SELECT COUNT(*) FROM {S}.machines)",
                     "gold_bottom_ten": "40", "gold_zone_daily": f"(SELECT COUNT(*) * 7 FROM {S}.calendar_day)",
                     "gold_property_daily": f"(SELECT COUNT(*) FROM {S}.calendar_day)",
                     "gold_downtime_tickets": f"(SELECT COUNT(*) FROM {S}.operations)",
                     "gold_forecast_daily": f"(SELECT COUNT(*) FROM {S}.calendar_day)", "gold_investment_plan": "4"}
    for spec in TABLES:
        name, keys = spec["name"], ", ".join(spec["keys"])
        if name in expected_rows:
            check(name, "row count matches its source", "row count",
                  f"SELECT COUNT(*) - {expected_rows[name]} AS observed FROM {G}.{name}")
        else:
            check(name, "table is not empty", "row count", f"SELECT CAST(COUNT(*) > 0 AS INT) AS observed FROM {G}.{name}",
                  expected=1)
        check(name, f"key ({keys}) unique", "keys", f"SELECT COUNT(*) - COUNT(DISTINCT {keys}) AS observed FROM {G}.{name}")
        check(name, f"key ({keys}) not null", "keys",
              f"SELECT COUNT(*) AS observed FROM {G}.{name} WHERE {' OR '.join(f'{k} IS NULL' for k in spec['keys'])}")

    #lineage and freshness
    hash_rows = ", ".join(f"({lit(n)}, {lit(h)})" for n, h in sorted(local_hashes.items()))
    check("volume", "uploaded analysis outputs are byte-identical to local files (sha-256)", "lineage",
          f"WITH l AS (SELECT * FROM VALUES {hash_rows} AS l(name, sha)), "
          f"r AS (SELECT regexp_extract(path, '([^/]+)$', 1) AS name, sha2(content, 256) AS sha "
          f"FROM read_files('{VOLUME_PATH}/', format => 'binaryFile')) "
          f"SELECT COUNT(*) AS observed, concat_ws('; ', slice(collect_list(l.name), 1, 8)) AS detail "
          f"FROM l LEFT JOIN r ON r.name = l.name WHERE r.sha IS NULL OR r.sha <> l.sha")
    check("volume", "no stale files in the volume that are absent locally", "lineage",
          f"SELECT COUNT(*) AS observed, concat_ws('; ', slice(collect_list(name), 1, 8)) AS detail FROM "
          f"(SELECT regexp_extract(path, '([^/]+)$', 1) AS name FROM read_files('{VOLUME_PATH}/', format => 'binaryFile')) "
          f"WHERE name NOT IN ({', '.join(lit(n) for n in local_hashes)})", severity="warn")
    check("outputs", "every python validation check passed", "lineage",
          f"SELECT COUNT(*) AS observed, concat_ws('; ', slice(collect_list(concat(f, ': ', `check`)), 1, 8)) AS detail FROM "
          f"(SELECT regexp_extract(_metadata.file_path, '([^/]+)$', 1) AS f, `check`, CAST(passed AS STRING) AS passed "
          f"FROM read_files('{VOLUME_PATH}/*validation-checks.csv', format => 'csv', header => true, escape => '\"')) "
          f"WHERE lower(passed) <> 'true'")
    check("*", "every gold table rebuilt after the latest silver change", "freshness",
          f"SELECT COUNT(*) AS observed, concat_ws('; ', slice(collect_list(table_name), 1, 8)) AS detail "
          f"FROM workspace.information_schema.tables WHERE table_schema = 'ante_gold' AND table_name IN "
          f"({', '.join(lit(t) for t in GOLD_TABLES)}) AND last_altered < (SELECT MAX(last_altered) "
          f"FROM workspace.information_schema.tables WHERE table_schema = 'ante_silver')")
    if build_start:
        check("*", "every gold table rebuilt in this run", "freshness",
              f"SELECT {len(GOLD_TABLES)} - COUNT(*) AS observed FROM workspace.information_schema.tables "
              f"WHERE table_schema = 'ante_gold' AND table_name IN ({', '.join(lit(t) for t in GOLD_TABLES)}) "
              f"AND last_altered >= TIMESTAMP{lit(build_start)} - INTERVAL 5 MINUTES")
    for name in ["gold_machine_daily", "gold_zone_daily", "gold_property_daily", "gold_forecast_daily"]:
        check(name, "gaming dates span 2025-01-01 to 2025-06-30", "freshness",
              f"SELECT CAST(MIN(gaming_date) = DATE'2025-01-01' AND MAX(gaming_date) = DATE'2025-06-30' AS INT) AS observed "
              f"FROM {G}.{name}", expected=1)
    check("*", "no unexpected tables in ante_gold", "lineage",
          f"SELECT COUNT(*) AS observed, concat_ws('; ', collect_list(table_name)) AS detail "
          f"FROM workspace.information_schema.tables WHERE table_schema = 'ante_gold' AND table_name NOT IN "
          f"({', '.join(lit(t) for t in GOLD_TABLES + [LOG_TABLE])})")

    #privacy
    check("*", "no player-level identifier columns in gold", "privacy",
          f"SELECT COUNT(*) AS observed, concat_ws('; ', collect_list(concat(table_name, '.', column_name))) AS detail "
          f"FROM workspace.information_schema.columns WHERE table_schema = 'ante_gold' "
          f"AND lower(column_name) IN ({', '.join(lit(c) for c in FORBIDDEN_COLUMNS)})")
    check("gold_player_segments", f"no published cell with fewer than {MIN_CELL} players", "privacy",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_player_segments WHERE NOT suppressed AND (players_seen < {MIN_CELL} "
          f"OR (players_with_play > 0 AND players_with_play < {MIN_CELL}))")
    check("gold_player_segments", "suppressed cells expose no measures", "privacy",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_player_segments WHERE suppressed AND "
          f"({' OR '.join(f'{m} IS NOT NULL' for m in SEGMENT_MEASURES)})")
    check("gold_player_segments", "no dimension has exactly one suppressed cell (subtraction guard)", "privacy",
          f"SELECT COUNT(*) AS observed FROM (SELECT period_month, dimension FROM {G}.gold_player_segments "
          f"GROUP BY period_month, dimension HAVING COUNT_IF(suppressed) = 1)")
    check("gold_player_segments", "suppressed cells (count, for review)", "privacy",
          f"SELECT COUNT_IF(suppressed) AS observed, concat_ws('; ', slice(collect_list(CASE WHEN suppressed THEN "
          f"concat(period_month, ' ', dimension, '=', segment) END), 1, 12)) AS detail FROM {G}.gold_player_segments",
          severity="info")

    #q1 machine reconciliation
    md = (f"(SELECT machine_id, SUM(coin_in_usd) AS coin, SUM(house_win_usd) AS hw, SUM(theo_usd) AS theo, "
          f"SUM(available_hours) AS ah, SUM(scheduled_hours) AS sh, SUM(occupied_hours) AS oh, SUM(paid_spins) AS spins, "
          f"SUM(hit_spins) AS hits, SUM(contribution_usd) AS contrib, COUNT(*) AS days, "
          f"SUM(luck_residual_usd) / sqrt(SUM(luck_variance_usd2)) AS luck_z, "
          f"(SUM(hit_spins) - SUM(expected_hit_spins)) / sqrt(SUM(hit_variance)) AS hit_z "
          f"FROM {G}.gold_machine_daily GROUP BY machine_id)")
    check("gold_machine_daily", "totals equal silver on-floor totals (coin-in, house win, theo)", "reconciliation",
          f"SELECT COUNT(*) AS observed FROM (SELECT SUM(coin_in_usd) c, SUM(house_win_usd) h, SUM(theo_usd) t FROM {G}.gold_machine_daily) g "
          f"CROSS JOIN (SELECT SUM(coin_in_usd) c, SUM(house_win_usd) h, SUM(theo_usd) t FROM {S}.prepared_daily_view WHERE active) s "
          f"WHERE g.c <> s.c OR g.h <> s.h OR g.t <> s.t")
    mismatches("gold_machine_scorecard", "per-machine sums from gold_machine_daily match machine-performance.csv "
               "(coin-in, house win, theo, contribution, hours, spins, days)",
               f"{md} g FULL JOIN {G}.gold_machine_scorecard s ON s.machine_id = g.machine_id",
               " OR ".join([differs("g.coin", "s.coin_in_usd"), differs("g.hw", "s.house_win_usd"),
                            differs("g.theo", "s.theo_usd"), differs("g.contrib", "s.contribution_usd"),
                            differs("g.ah", "s.available_hours", 1e-6), differs("g.sh", "s.scheduled_hours", 1e-6),
                            differs("g.oh", "s.occupied_hours", 1e-6), "g.spins <> s.paid_spins",
                            "g.hits <> s.hit_spins", "g.days <> s.days_on_floor"]), "coalesce(g.machine_id, s.machine_id)")
    mismatches("gold_machine_scorecard", "luck z and hit z recomputed in sql match machine-performance.csv",
               f"{md} g JOIN {G}.gold_machine_scorecard s ON s.machine_id = g.machine_id",
               f"{rel_differs('g.luck_z', 's.luck_z')} OR {rel_differs('g.hit_z', 's.hit_z')}", "s.machine_id")
    check("gold_machine_scorecard", "M-1158 hit z recomputed from silver meters (reported 124.19)", "reconciliation",
          f"SELECT round(hit_z, 2) AS observed FROM {md} WHERE machine_id = 'M-1158'", expected=124.19)
    check("gold_machine_scorecard", "M-1158 luck z recomputed from silver meters (reported -4.57)", "reconciliation",
          f"SELECT round(luck_z, 2) AS observed FROM {md} WHERE machine_id = 'M-1158'", expected=-4.57)
    check("gold_machine_scorecard", "M-1158 is the only machine with |hit z| > 4", "reconciliation",
          f"SELECT concat_ws(',', collect_list(machine_id)) AS observed FROM {G}.gold_machine_scorecard WHERE flag_payout_pattern",
          expected="M-1158")
    check("gold_machine_scorecard", "manager's bottom 10 recomputed in sql from gold_machine_daily", "reconciliation",
          f"SELECT COUNT(*) AS observed FROM ((SELECT machine_id FROM {md} ORDER BY hw LIMIT 10) EXCEPT "
          f"(SELECT machine_id FROM {G}.gold_machine_scorecard WHERE in_original_bottom_10))")
    check("gold_machine_scorecard", "downtime join found every machine", "reconciliation",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_machine_scorecard WHERE downtime_hours IS NULL")
    check("gold_machine_scorecard", "machines requiring investigation (for review)", "review",
          f"SELECT COUNT_IF(requires_investigation) AS observed, concat_ws('; ', collect_list(CASE WHEN requires_investigation "
          f"THEN concat(machine_id, ': ', review_notes) END)) AS detail FROM {G}.gold_machine_scorecard", severity="info")
    check("gold_bottom_ten", "each list has exactly 10 machines", "invariant",
          f"SELECT COUNT(*) AS observed FROM (SELECT list_name FROM {G}.gold_bottom_ten GROUP BY list_name HAVING COUNT(*) <> 10)")
    check("gold_bottom_ten", "original list members still in the adjusted demand list (for review)", "review",
          f"SELECT COUNT(*) AS observed, concat_ws(', ', collect_list(machine_id)) AS detail FROM {G}.gold_bottom_ten "
          f"WHERE list_name LIKE 'demand-adjusted%' AND in_original_bottom_10", severity="info")

    #q2 zone and property reconciliation
    mismatches("gold_zone_daily", "zone-day sums match demand-zone-daily.csv (coin-in, theo, house win, hours, "
               "machines, entries, coverage, period)",
               f"{G}.gold_zone_daily g FULL JOIN {src('demand-zone-daily.csv')} d ON d.zone_id = g.zone_id "
               f"AND CAST(d.gaming_date AS DATE) = g.gaming_date",
               " OR ".join(["g.zone_id IS NULL", "d.zone_id IS NULL", differs("g.coin_in_usd", "d.coin_in_usd"), differs("g.theo_usd", "d.theo_usd"),
                            differs("g.house_win_usd", "d.house_win_usd"),
                            differs("g.carded_coin_in_usd", "d.carded_coin_in_usd"),
                            differs("g.available_hours", "d.available_hours", 1e-6),
                            differs("g.occupied_hours", "d.occupied_hours", 1e-6),
                            differs("g.scheduled_machine_hours", "d.scheduled_machine_hours", 1e-6),
                            "g.machines <> d.machines", differs("g.entries_observed", "d.entries_observed", 0),
                            differs("g.covered_sensor_hours", "d.covered_hours", 1e-6),
                            "g.offline_sensor_hours <> d.offline_hours", "g.closure_period <> d.period",
                            "g.faults_opened <> d.faults_opened"]),
               "concat(coalesce(g.zone_id, d.zone_id), ' ', CAST(coalesce(g.gaming_date, d.gaming_date) AS STRING))")
    mismatches("gold_zone_daily", "zones add up to the property every day (no double counting)",
               f"(SELECT gaming_date, SUM(coin_in_usd) c, SUM(theo_usd) t, SUM(house_win_usd) h, SUM(available_hours) a, "
               f"SUM(machines) m FROM {G}.gold_zone_daily GROUP BY gaming_date) z JOIN {G}.gold_property_daily p USING (gaming_date)",
               f"z.c <> p.coin_in_usd OR z.t <> p.theo_usd OR z.h <> p.house_win_usd OR z.m <> p.active_machines OR "
               f"{differs('z.a', 'p.available_machine_hours', 1e-6)}", "gaming_date")
    mismatches("gold_property_daily", "property days match demand-property-daily.csv",
               f"{G}.gold_property_daily g FULL JOIN {src('demand-property-daily.csv')} d ON CAST(d.gaming_date AS DATE) = g.gaming_date",
               " OR ".join(["g.gaming_date IS NULL", "d.gaming_date IS NULL", differs("g.coin_in_usd", "d.coin_in_usd"), differs("g.theo_usd", "d.theo_usd"),
                            differs("g.house_win_usd", "d.house_win_usd"), "g.active_machines <> d.active_machines",
                            differs("g.available_machine_hours", "d.available_machine_hours", 1e-6),
                            differs("g.total_zone_entries_observed", "d.entries_observed", 0),
                            "g.closure_period <> d.period"]),
               "coalesce(g.gaming_date, d.gaming_date)")
    mismatches("gold_property_daily", "comp, f&b and points match loyalty-daily.csv",
               f"{G}.gold_property_daily g FULL JOIN {src('loyalty-daily.csv')} d ON CAST(d.gaming_date AS DATE) = g.gaming_date",
               " OR ".join(["g.gaming_date IS NULL", "d.gaming_date IS NULL", differs("g.comp_cost_usd", "d.comp_cost_usd"), differs("g.comp_face_value_usd", "d.comp_face_value_usd"),
                            differs("g.pos_cogs_usd", "d.pos_cogs_usd"), differs("g.pos_cash_usd", "d.pos_cash_usd"),
                            "g.redemptions <> d.redemptions", "g.points_redeemed <> d.points_redeemed",
                            "g.points_earned <> d.points_earned", differs("g.carded_theo_usd", "d.carded_theo_usd"),
                            differs("g.carded_coin_in_usd", "d.carded_coin_in_usd"),
                            "g.carded_cash_tickets <> d.carded_cash_tickets", differs("g.carded_cash_usd", "d.carded_cash_usd"),
                            "g.uncarded_tickets <> d.uncarded_tickets", differs("g.uncarded_cash_usd", "d.uncarded_cash_usd"),
                            "g.comp_policy_period <> d.policy_period"]),
               "coalesce(g.gaming_date, d.gaming_date)")
    mismatches("gold_property_daily", "scheduled and downtime hours recomputed in sql match downtime-daily.csv",
               f"{G}.gold_property_daily g FULL JOIN {src('downtime-daily.csv')} d ON CAST(d.gaming_date AS DATE) = g.gaming_date",
               f"g.gaming_date IS NULL OR d.gaming_date IS NULL OR {differs('g.scheduled_machine_hours', 'd.scheduled_hours', 1e-6)} OR {differs('g.downtime_hours', 'd.downtime_hours', 1e-6)}",
               "coalesce(g.gaming_date, d.gaming_date)")
    mismatches("gold_property_daily", "machine-days add up to silver property_day",
               f"(SELECT gaming_date, SUM(coin_in_usd) c, SUM(house_win_usd) h, SUM(theo_usd) t FROM {G}.gold_machine_daily "
               f"GROUP BY gaming_date) m JOIN {S}.property_day p USING (gaming_date)",
               "m.c <> p.coin_in_usd OR m.h <> p.house_win_usd OR m.t <> p.theo_usd", "gaming_date")
    check("gold_property_daily", "downtime causes add up to observed downtime hours", "reconciliation",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_property_daily WHERE "
          + differs("downtime_fault_repair_hours + downtime_move_hours + downtime_scheduled_maintenance_hours + "
                    "downtime_install_hours + downtime_retire_hours", "downtime_hours", 1e-6))
    check("gold_property_daily", "comp cost counted once: non-comp cogs + comp cost = pos cogs", "money rules",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_property_daily WHERE non_comp_cogs_usd + comp_cost_usd <> pos_cogs_usd "
          f"OR non_comp_cogs_usd < 0")
    check("gold_property_daily", "comp cost total equals silver redemption economic cost", "money rules",
          f"SELECT CAST(round((SELECT SUM(comp_cost_usd) FROM {G}.gold_property_daily) * 100) - (SELECT SUM(economic_cost_cents) "
          f"FROM {S}.loyalty_ledger WHERE event_type = 'redeem') AS BIGINT) AS observed")
    check("gold_property_daily", "comp face value is never booked as cash", "money rules",
          f"SELECT COUNT(*) AS observed FROM {S}.fnb_transactions WHERE is_comp AND cash_receipt_cents <> 0")

    #q3 downtime
    mismatches("gold_downtime_tickets", "every ticket matches silver operations (date, machine, cause, hours)",
               f"{G}.gold_downtime_tickets g FULL JOIN {S}.operations o ON o.operation_id = g.operation_id",
               " OR ".join(["g.operation_id IS NULL", "o.operation_id IS NULL", "g.opened_gaming_date <> o.opened_gaming_date",
                            "g.machine_id <> o.machine_id", "g.cause <> o.event_type",
                            differs("g.total_hours", "o.unavailable_seconds / 3600.0", 1e-6),
                            differs("g.direct_cost_usd", "o.direct_cost_cents / 100.0")]),
               "coalesce(g.operation_id, o.operation_id)")
    check("gold_downtime_tickets", "ticket hours by cause equal the daily cause allocation", "reconciliation",
          f"SELECT COUNT(*) AS observed FROM (SELECT SUM(CASE WHEN cause = 'fault_repair' THEN total_hours END) f, "
          f"SUM(CASE WHEN cause = 'move' THEN total_hours END) mv, SUM(CASE WHEN cause = 'install' THEN total_hours END) i, "
          f"SUM(CASE WHEN cause = 'scheduled_maintenance' THEN total_hours END) sm, SUM(waiting_hours) w "
          f"FROM {G}.gold_downtime_tickets) t CROSS JOIN (SELECT SUM(downtime_fault_repair_hours) f, SUM(downtime_move_hours) mv, "
          f"SUM(downtime_install_hours) i, SUM(downtime_scheduled_maintenance_hours) sm, SUM(downtime_fault_waiting_hours) w "
          f"FROM {G}.gold_property_daily) p WHERE "
          + " OR ".join(differs(f"t.{c}", f"p.{c}", 1e-6) for c in ["f", "mv", "i", "sm", "w"]))
    check("gold_downtime_tickets", "estimated lost theo equals the gross lost-play estimate", "reconciliation",
          f"SELECT COUNT(*) AS observed FROM (SELECT SUM(est_gross_lost_theo_usd) t, SUM(est_gross_lost_coin_in_usd) c "
          f"FROM {G}.gold_downtime_tickets) g CROSS JOIN (SELECT MAX(CASE WHEN measure = 'gross lost expected win (theo)' "
          f"AND method LIKE 'zone%' THEN estimate_usd END) t, MAX(CASE WHEN measure = 'gross lost coin-in' AND method LIKE 'zone%' "
          f"THEN estimate_usd END) c FROM {src('downtime-lost-play.csv')}) l WHERE {differs('g.t', 'l.t')} OR {differs('g.c', 'l.c')}")
    check("gold_downtime_tickets", "observed downtime equals downtime-summary.csv", "reconciliation",
          f"SELECT COUNT(*) AS observed FROM (SELECT SUM(downtime_hours) d, SUM(scheduled_hours) s FROM {G}.gold_machine_daily) g "
          f"CROSS JOIN {src('downtime-summary.csv')} x WHERE {differs('g.d', 'x.downtime_hours', 1e-6)} "
          f"OR {differs('g.s', 'x.scheduled_hours', 1e-6)}")

    #q4 and q5 segments
    seg_all = f"(SELECT * FROM {G}.gold_player_segments WHERE dimension = 'all' AND period_month <> 'ALL')"
    mismatches("gold_player_segments", "monthly totals match players-monthly.csv",
               f"{seg_all} g FULL JOIN {src('players-monthly.csv')} d ON date_format(d.period_month, 'yyyy-MM') = g.period_month",
               " OR ".join(["g.period_month IS NULL", "d.period_month IS NULL", "g.players_seen <> d.players_seen", "g.players_with_play <> d.players_with_play",
                            "g.players_ahead <> d.players_ahead", differs("g.coin_in_usd", "d.coin_in_usd"),
                            differs("g.payout_usd", "d.payout_usd"), differs("g.net_usd", "d.net_usd"),
                            differs("g.theo_usd", "d.theo_usd"), differs("g.luck_residual_usd", "d.luck_residual_usd"),
                            "g.observed_visits <> d.observed_visits", "g.sessions <> d.sessions",
                            differs("g.play_hours", "d.play_hours", 1e-6)]),
               "coalesce(g.period_month, date_format(d.period_month, 'yyyy-MM'))")
    mismatches("gold_player_segments", "monthly comps match loyalty-monthly.csv",
               f"{seg_all} g FULL JOIN {src('loyalty-monthly.csv')} d ON date_format(d.period_month, 'yyyy-MM') = g.period_month",
               " OR ".join(["g.period_month IS NULL", "d.period_month IS NULL", differs("g.comp_cost_usd", "d.comp_cost_usd"), differs("g.comp_face_value_usd", "d.comp_face_value_usd"),
                            "g.comps_redeemed <> d.redemptions", "g.points_redeemed <> d.points_redeemed",
                            "g.points_earned <> d.points_earned"]),
               "coalesce(g.period_month, date_format(d.period_month, 'yyyy-MM'))")
    group_map = ("CASE group_type WHEN 'opening tier' THEN 'opening_tier' WHEN 'home region' THEN 'home_region' "
                 "WHEN 'enrollment cohort' THEN 'enrollment_cohort' END")
    mismatches("gold_player_segments", "6-month segments match players-groups.csv (tier, region, cohort)",
               f"(SELECT {group_map} AS dimension, `group` AS segment, * EXCEPT (`group`) FROM {src('players-groups.csv')} "
               f"WHERE group_type IN ('opening tier', 'home region', 'enrollment cohort')) d "
               f"LEFT JOIN {G}.gold_player_segments g ON g.period_month = 'ALL' AND g.dimension = d.dimension AND g.segment = d.segment",
               " OR ".join(["g.segment IS NULL", "g.suppressed", "g.players_with_play <> d.players",
                            differs("g.coin_in_usd", "d.coin_in_usd"), differs("g.payout_usd", "d.payout_usd"),
                            differs("g.theo_usd", "d.theo_usd"), differs("g.house_win_usd", "d.house_win_usd"),
                            differs("g.player_losses_usd", "d.loss_usd"), differs("g.player_winnings_usd", "d.win_usd"),
                            "g.players_ahead <> d.players_ahead", "g.observed_visits_players_with_play <> d.visits",
                            differs("g.comp_cost_usd", "d.comp_cost_usd")]),
               "concat(d.dimension, '=', d.segment)")
    mismatches("gold_player_segments", "6-month segments match loyalty-groups.csv (tier, region, cohort)",
               f"(SELECT {group_map} AS dimension, `group` AS segment, * EXCEPT (`group`) FROM {src('loyalty-groups.csv')} "
               f"WHERE group_type IN ('opening tier', 'home region', 'enrollment cohort')) d "
               f"LEFT JOIN {G}.gold_player_segments g ON g.period_month = 'ALL' AND g.dimension = d.dimension AND g.segment = d.segment",
               " OR ".join(["g.segment IS NULL", "g.players_seen <> d.players", differs("g.comp_cost_usd", "d.comp_cost_usd"),
                            differs("g.comp_face_value_usd", "d.comp_face_usd"), "g.comps_redeemed <> d.comps",
                            "g.points_earned <> d.points_earned", "g.points_redeemed <> d.points_redeemed"]),
               "concat(d.dimension, '=', d.segment)")
    check("gold_player_segments", "6-month totals match players-summary.csv and loyalty-summary.csv", "reconciliation",
          f"SELECT COUNT(*) AS observed FROM (SELECT * FROM {G}.gold_player_segments WHERE period_month = 'ALL' AND dimension = 'all') g "
          f"CROSS JOIN {src('players-summary.csv')} p CROSS JOIN {src('loyalty-summary.csv')} l WHERE "
          + " OR ".join(["g.players_seen <> p.players_enrolled", "g.players_with_play <> p.players_with_carded_play",
                         "g.players_ahead <> p.players_ahead", differs("g.coin_in_usd", "p.carded_coin_in_usd"),
                         differs("g.net_usd", "p.player_net_usd"), differs("g.theo_usd", "p.theo_usd"),
                         differs("g.player_losses_usd", "p.total_player_losses_usd"),
                         differs("g.comp_cost_usd", "l.comp_cost_usd"), differs("g.comp_face_value_usd", "l.comp_face_value_usd"),
                         "g.comps_redeemed <> l.comps_redeemed"]))
    mismatches("gold_player_segments", "segments add up to the total where nothing is suppressed",
               f"(SELECT period_month, dimension, SUM(coin_in_usd) c, SUM(players_seen) n, COUNT_IF(suppressed) s "
               f"FROM {G}.gold_player_segments WHERE dimension <> 'all' GROUP BY period_month, dimension) x "
               f"JOIN (SELECT period_month, coin_in_usd, players_seen FROM {G}.gold_player_segments WHERE dimension = 'all') t "
               f"USING (period_month)",
               f"x.s = 0 AND ({differs('x.c', 't.coin_in_usd')} OR x.n <> t.players_seen)",
               "concat(period_month, ' ', dimension)")

    #forecast
    mismatches("gold_forecast_daily", "actual coin-in equals silver property_day every day",
               f"{G}.gold_forecast_daily f FULL JOIN {S}.property_day p USING (gaming_date)",
               differs("f.actual_coin_in_usd", "p.coin_in_usd"), "gaming_date")
    check("gold_forecast_daily", "61 holdout days at horizons 1-61 from origin 2025-04-30", "invariant",
          f"SELECT CAST(COUNT(*) = 61 AND MIN(horizon_days) = 1 AND MAX(horizon_days) = 61 AND MIN(gaming_date) = DATE'2025-05-01' "
          f"AS INT) AS observed FROM {G}.gold_forecast_daily WHERE forecast_origin = DATE'2025-04-30'", expected=1)
    mismatches("gold_model_validation", "holdout mae recomputed from daily forecasts matches forecast-performance.csv",
               f"(SELECT stack(3, 'constant mean', AVG(abs_error_constant_mean_usd), 'weekday mean', AVG(abs_error_weekday_mean_usd), "
               f"'ridge (selected)', AVG(abs_error_ridge_usd)) AS (model, mae) FROM {G}.gold_forecast_daily "
               f"WHERE forecast_origin IS NOT NULL) r LEFT JOIN {G}.gold_model_validation v ON v.model = r.model "
               f"AND v.metric = 'mae' AND v.section LIKE 'holdout%'", rel_differs("r.mae", "v.value", 1e-9), "r.model")
    check("gold_model_validation", "interval flags agree with the interval bounds", "invariant",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_forecast_daily WHERE forecast_origin IS NOT NULL AND actual_in_ridge_interval <> "
          f"(actual_coin_in_usd >= forecast_ridge_low_usd AND actual_coin_in_usd <= forecast_ridge_high_usd)")
    check("gold_model_validation", "disclosure names the weekday-mean baseline as the most accurate on the holdout",
          "disclosure", f"SELECT model AS observed FROM {G}.gold_model_validation WHERE section = 'disclosure'",
          expected="weekday mean")
    check("gold_model_validation", "ridge 90% interval coverage on the holdout (nominal 0.90)", "disclosure",
          f"SELECT round(value, 3) AS observed, note AS detail FROM {G}.gold_model_validation "
          f"WHERE section LIKE 'interval calibration%'", severity="info")

    #q6
    check("gold_investment_plan", "plan cost $6,600, unallocated $93,400, within budget", "budget",
          f"SELECT CAST(SUM(target_cost_usd) = 6600 AND MIN(unallocated_usd) = 93400 AND MAX(plan_total_cost_usd) <= {BUDGET_USD} "
          f"AS INT) AS observed FROM {G}.gold_investment_plan", expected=1)
    check("gold_investment_plan", "at most 3 action types funded", "budget",
          f"SELECT CAST(COUNT(DISTINCT action_type_id) <= 3 AS INT) AS observed FROM {G}.gold_investment_plan", expected=1)
    check("gold_investment_plan", "target costs sum to each action's catalog total", "budget",
          f"SELECT COUNT(*) AS observed FROM (SELECT action_type_id, SUM(target_cost_usd) c, MAX(action_total_cost_usd) t, "
          f"COUNT(*) n, MAX(action_quantity) q FROM {G}.gold_investment_plan GROUP BY action_type_id) WHERE c <> t OR n <> q")
    check("gold_investment_plan", "plan net low/central/high equals the recommended alternative", "reconciliation",
          f"SELECT COUNT(*) AS observed FROM (SELECT SUM(net_low_usd) l, SUM(net_central_usd) c, SUM(net_high_usd) h "
          f"FROM {G}.gold_investment_plan) g CROSS JOIN (SELECT * FROM {src('investment-alternatives.csv')} WHERE plan = 'recommended') a "
          f"WHERE {differs('g.l', 'a.net_low_usd')} OR {differs('g.c', 'a.net_central_usd')} OR {differs('g.h', 'a.net_high_usd')}")
    check("gold_investment_plan", "plan benefit equals the recommended plan by action", "reconciliation",
          f"SELECT COUNT(*) AS observed FROM (SELECT action_type_id, SUM(benefit_low_usd) l, SUM(benefit_central_usd) c, "
          f"SUM(benefit_high_usd) h, SUM(target_downtime_cost_usd) d FROM {G}.gold_investment_plan GROUP BY action_type_id) g "
          f"JOIN {src('investment-recommended-plan.csv')} p USING (action_type_id) WHERE "
          + " OR ".join([differs("g.l", "p.benefit_low_usd"), differs("g.c", "p.benefit_central_usd"),
                         differs("g.h", "p.benefit_high_usd"), differs("g.d", "p.downtime_cost_usd")]))
    check("gold_investment_plan", "inspection carries no financial benefit in any scenario", "money rules",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_investment_plan WHERE action_type_id = 'A-INSPECT' AND "
          f"(benefit_low_usd <> 0 OR benefit_central_usd <> 0 OR benefit_high_usd <> 0 OR funding_basis <> 'information')")
    check("gold_investment_plan", "every funded action is evidence-supported and robust or verifiable", "budget",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_investment_plan WHERE NOT evidence_supports_funding OR NOT robust_or_verifiable")
    check("gold_investment_plan", "investment-move-plan.csv net_* columns hold benefit before move cost and downtime "
          "(label differs from investment-candidates.csv)", "label",
          f"SELECT COUNT(*) AS observed, 'gold uses them as benefit_* and computes net = benefit - cost - downtime; "
          f"the sum matches investment-recommended-plan benefit_*, not net_*' AS detail "
          f"FROM (SELECT SUM(CAST(net_low_usd AS DOUBLE)) l FROM {src('investment-move-plan.csv')}) m "
          f"CROSS JOIN (SELECT * FROM {src('investment-recommended-plan.csv')} WHERE action_type_id = 'A-MOVE-MACHINE') p "
          f"WHERE abs(m.l - p.benefit_low_usd) < 0.01", severity="warn")
    check("gold_scenarios", "infeasible alternatives exceed the budget and feasible ones do not", "budget",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_scenarios WHERE scenario_group LIKE 'budget alternatives%' AND "
          f"(feasible AND cost_usd > {BUDGET_USD} OR NOT feasible AND cost_usd <= {BUDGET_USD})")
    check("gold_scenarios", "hypothetical inspection recovery is never in the recommended plan", "money rules",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_scenarios WHERE kind LIKE 'hypothetical%' AND in_recommended_plan")

    #evidence labelling
    check("gold_evidence", "every row has a class, causal reading and source", "labelling",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_evidence WHERE evidence_class IS NULL OR causal_interpretation IS NULL "
          f"OR source_file IS NULL OR scope IS NULL")
    check("gold_evidence", "associations are never labelled causal", "labelling",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_evidence WHERE lower(evidence_class) LIKE '%association%' "
          f"AND causal_interpretation NOT LIKE 'not causal%' AND causal_interpretation NOT LIKE 'before/after association%'")
    mismatches("gold_evidence", "M-1158 hit z and luck z in evidence equal the sql recomputation",
               f"{G}.gold_evidence e CROSS JOIN {md} g", f"g.machine_id = 'M-1158' AND e.subgroup = 'M-1158' AND "
               f"((e.metric LIKE 'hit-rate z%' AND {rel_differs('e.estimate', 'g.hit_z')}) OR "
               f"(e.metric LIKE 'luck z%' AND {rel_differs('e.estimate', 'g.luck_z')}))", "e.metric")
    check("gold_scenarios", "every scenario is labelled as a scenario or counterfactual estimate", "labelling",
          f"SELECT COUNT(*) AS observed FROM {G}.gold_scenarios WHERE kind IS NULL OR lower(kind) LIKE 'observed%'")


#=== run ===

class GoldWarehouse(bronze.Warehouse):
    def query(self, statement):
        #typed result with chunk paging, for snapshot export
        for attempt in range(1, bronze.MAX_ATTEMPTS + 1):
            try:
                return self._query_once(statement)
            except bronze.SqlError as exc:
                if not exc.retryable or attempt == bronze.MAX_ATTEMPTS:
                    raise
                time.sleep(5 * 2 ** (attempt - 1))

    def _query_once(self, statement):
        response = self._cli(["api", "post", "/api/2.0/sql/statements"],
                             {"statement": statement, "warehouse_id": self.warehouse_id, "wait_timeout": "30s",
                              "on_wait_timeout": "CONTINUE", "disposition": "INLINE", "format": "JSON_ARRAY"})
        statement_id = response.get("statement_id")
        started = time.monotonic()
        while response.get("status", {}).get("state") not in bronze.TERMINAL_STATES:
            if time.monotonic() - started > bronze.STATEMENT_TIMEOUT_S:
                self._cli(["api", "post", f"/api/2.0/sql/statements/{statement_id}/cancel"], {})
                raise bronze.SqlError(f"statement {statement_id} timed out")
            time.sleep(2)
            response = self._cli(["api", "get", f"/api/2.0/sql/statements/{statement_id}"])
        status = response["status"]
        if status["state"] != "SUCCEEDED":
            error = status.get("error", {})
            message = f"{status['state']} {error.get('error_code', '')}: {error.get('message', '')}"
            raise bronze.SqlError(message, retryable=bool(bronze.RETRYABLE.search(message)))
        manifest = response.get("manifest", {})
        if manifest.get("truncated"):
            raise bronze.SqlError("result truncated; reduce the page size")
        result = response.get("result", {})
        rows = list(result.get("data_array") or [])
        link = result.get("next_chunk_internal_link")
        while link:
            chunk = self._cli(["api", "get", link])
            rows.extend(chunk.get("data_array") or [])
            link = chunk.get("next_chunk_internal_link")
        return manifest["schema"]["columns"], rows


def run_check(warehouse, spec):
    record = {k: spec[k] for k in ("table", "check", "category", "severity", "expected")}
    try:
        row = warehouse.execute(spec["sql"])[0]
        observed = row.get("observed")
        expected = spec["expected"]
        if isinstance(expected, (int, float)) and observed is not None:
            try:
                observed = float(observed)
                observed = int(observed) if observed.is_integer() else observed
            except ValueError:
                pass
        passed = observed == expected or (observed is None and expected == 0)
        status = "PASS" if passed else {"error": "FAIL", "warn": "WARN", "info": "INFO"}[spec["severity"]]
        if spec["severity"] == "info":
            status = "INFO"
        record.update(observed=observed, status=status, detail=(row.get("detail") or "")[:600])
    except bronze.SqlError as exc:
        record.update(observed="", status="FAIL", detail=f"query error: {str(exc)[:600]}")
    return record


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def upload_outputs(cli, profile):
    target = f"dbfs:{VOLUME_PATH}"
    done = subprocess.run([cli, "fs", "cp", "-r", str(OUTPUTS), target, "--overwrite", "-p", profile],
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    if done.returncode != 0:
        sys.exit(f"upload to {target} failed: {(done.stderr or done.stdout).strip()}")


def freshness_records():
    #outputs must be newer than the script that writes them
    records = []
    for script, prefixes in SCRIPT_PREFIXES.items():
        script_time = (ROOT / script).stat().st_mtime
        stale = [p.name for p in sorted(OUTPUTS.glob("*.csv"))
                 if any(p.name.startswith(prefix) for prefix in prefixes) and p.stat().st_mtime < script_time]
        records.append({"table": "outputs", "check": f"outputs of {script} are newer than the script",
                        "category": "freshness", "severity": "warn", "expected": 0, "observed": len(stale),
                        "status": "PASS" if not stale else "WARN", "detail": "; ".join(stale[:8])})
    return records


def to_arrow(columns, rows):
    arrays, fields = [], []
    for i, column in enumerate(columns):
        kind = column["type_name"]
        values = [row[i] for row in rows]
        if kind in ("LONG", "INT", "SHORT", "BYTE"):
            arrow_type, convert = pa.int64(), int
        elif kind in ("DOUBLE", "FLOAT"):
            arrow_type, convert = pa.float64(), float
        elif kind == "DECIMAL":
            arrow_type = pa.decimal128(int(column["type_precision"]), int(column["type_scale"]))
            convert = decimal.Decimal
        elif kind == "BOOLEAN":
            arrow_type, convert = pa.bool_(), lambda v: v == "true"
        elif kind == "DATE":
            arrow_type, convert = pa.date32(), date.fromisoformat
        elif kind.startswith("TIMESTAMP"):
            arrow_type, convert = pa.timestamp("us"), lambda v: datetime.fromisoformat(v.rstrip("Z"))
        elif kind == "STRING":
            arrow_type, convert = pa.string(), str
        else:
            raise ValueError(f"unsupported type {kind} for {column['name']}")
        arrays.append(pa.array([None if v is None else convert(v) for v in values], type=arrow_type))
        fields.append(pa.field(column["name"], arrow_type))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def export_snapshot(warehouse, build_id, built_at, keys_by_table):
    SNAPSHOT.mkdir(exist_ok=True)
    records, manifest_tables = [], []
    for name, keys in keys_by_table.items():
        tables, offset = [], 0
        while True:
            columns, rows = warehouse.query(f"SELECT * FROM {G}.{name} ORDER BY {', '.join(keys)} "
                                            f"LIMIT {SNAPSHOT_PAGE} OFFSET {offset}")
            tables.append(to_arrow(columns, rows))
            offset += len(rows)
            if len(rows) < SNAPSHOT_PAGE:
                break
        data = pa.concat_tables(tables)
        path = SNAPSHOT / f"{name}.parquet"
        pq.write_table(data, path, compression="zstd")

        #parity: row count and every numeric column sum, snapshot vs live gold
        numeric = [c["name"] for c in columns if c["type_name"] in ("LONG", "INT", "SHORT", "BYTE", "DOUBLE", "FLOAT", "DECIMAL")]
        live = warehouse.execute(f"SELECT COUNT(*) AS n{''.join(f', CAST(SUM(`{c}`) AS DOUBLE) AS `s_{c}`' for c in numeric)} "
                                 f"FROM {G}.{name}")[0]
        reread = pq.read_table(path)
        problems = []
        if reread.num_rows != int(live["n"]):
            problems.append(f"rows {reread.num_rows} vs {live['n']}")
        for c in numeric:
            local = sum(float(v) for v in reread.column(c).to_pylist() if v is not None)
            remote = float(live[f"s_{c}"]) if live[f"s_{c}"] is not None else 0.0
            if abs(local - remote) > 1e-6 * max(1.0, abs(remote)):
                problems.append(f"{c} sum {local} vs {remote}")
        leaked = [c for c in reread.column_names if c.lower() in FORBIDDEN_COLUMNS]
        if leaked:
            problems.append(f"player-level columns {leaked}")
        records.append({"table": name, "check": "snapshot parity: rows and numeric sums equal live gold; no player ids",
                        "category": "snapshot", "severity": "error", "expected": 0, "observed": len(problems),
                        "status": "FAIL" if problems else "PASS", "detail": "; ".join(problems)[:600]})
        manifest_tables.append({"table": name, "file": path.name, "rows": reread.num_rows, "sha256": sha256(path),
                                "keys": keys, "columns": [{"name": c["name"], "type": c["type_text"]} for c in columns]})
    manifest = {"build_id": build_id, "built_at": built_at, "source": G, "min_cell_players": MIN_CELL,
                "note": "aggregated gold tables only; no player-level records; regenerate with scripts/build-gold.py",
                "tables": manifest_tables}
    (SNAPSHOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="ante")
    parser.add_argument("--warehouse-name", default="Serverless Starter Warehouse")
    parser.add_argument("--checks-only", action="store_true", help="skip upload and rebuild; run checks only")
    parser.add_argument("--no-snapshot", action="store_true", help="skip the sanitized snapshot export")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    with open(SILVER_AUDIT, newline="", encoding="utf-8") as f:
        silver_failures = [r for r in csv.DictReader(f) if r["status"] == "FAIL"]
    if silver_failures:
        sys.exit(f"silver audit has {len(silver_failures)} failed checks; fix silver first")
    local_hashes = {p.name: sha256(p) for p in sorted(OUTPUTS.glob("*.csv"))}
    needed = {s.split("/", 1)[1] for t in TABLES for s in t["sources"] if s.startswith("outputs/")}
    missing = sorted(needed - set(local_hashes))
    if missing:
        sys.exit(f"missing analysis outputs: {missing}")

    cli = bronze.find_cli()
    warehouse = GoldWarehouse(cli, args.profile, bronze.resolve_warehouse(cli, args.profile, args.warehouse_name, None))
    build_id = uuid.uuid4().hex[:12]
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"gold build {build_id} at {built_at} UTC; warehouse {warehouse.warehouse_id}; profile {args.profile}")

    if not args.checks_only:
        warehouse.execute(f"CREATE SCHEMA IF NOT EXISTS {G}")
        warehouse.execute(f"CREATE VOLUME IF NOT EXISTS {VOLUME} COMMENT "
                          f"{lit('verbatim copies of the validated python analysis outputs (aggregated, no player-level rows)')}")
        upload_outputs(cli, args.profile)
        print(f"uploaded {len(local_hashes)} analysis outputs to {VOLUME_PATH}", flush=True)
        failures = []

        def build(spec):
            try:
                warehouse.execute(create_sql(spec, build_id, built_at))
                print(f"  built {G}.{spec['name']}", flush=True)
            except bronze.SqlError as exc:
                failures.append(spec["name"])
                print(f"  FAILED {G}.{spec['name']}: {exc}", flush=True)

        for wave in (1, 2):
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                list(pool.map(build, [t for t in TABLES if t["wave"] == wave]))
            if failures:
                sys.exit(f"gold tables failed to build: {failures}")

        columns = warehouse.execute(f"SELECT table_name, column_name FROM workspace.information_schema.columns "
                                    f"WHERE table_schema = 'ante_gold'")
        comments = {}
        for r in columns:
            if r["table_name"] in GOLD_TABLES and r["column_name"] in COLUMN_DOCS:
                comments.setdefault(r["table_name"], []).append(r["column_name"])

        #delta rejects concurrent metadata changes on one table, so alter each table serially
        def comment_table(name):
            for column in comments[name]:
                warehouse.execute(f"ALTER TABLE {G}.{name} ALTER COLUMN `{column}` COMMENT {lit(COLUMN_DOCS[column])}")

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            list(pool.map(comment_table, comments))
        print(f"applied {sum(map(len, comments.values()))} column comments", flush=True)

    add_checks(local_hashes, None if args.checks_only else built_at)
    print(f"running {len(CHECKS)} checks", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda c: run_check(warehouse, c), CHECKS))
    results.extend(freshness_records())

    counts = {r["name"]: int(warehouse.execute(f"SELECT COUNT(*) AS n FROM {G}.{r['name']}")[0]["n"]) for r in TABLES}
    status_counts = {}
    for record in results:
        status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1

    #lineage log in databricks: one row per gold table for this build
    log_rows = ", ".join(
        f"({lit(build_id)}, {lit(t['name'])}, {lit(built_at)}, {counts[t['name']]}, {lit(t['question'])}, {lit(t['grain'])}, "
        f"{lit(t['scope'])}, {lit(', '.join(t['sources']))}, {status_counts.get('PASS', 0)}, {status_counts.get('FAIL', 0)}, "
        f"{status_counts.get('WARN', 0)}, {status_counts.get('INFO', 0)})" for t in TABLES)
    warehouse.execute(
        f"CREATE OR REPLACE TABLE {G}.{LOG_TABLE} COMMENT 'lineage and check results of the latest gold build' "
        f"TBLPROPERTIES ('ante.layer' = 'gold', 'ante.build_id' = {lit(build_id)}, 'ante.contains_player_level' = 'false') AS "
        f"SELECT * FROM VALUES {log_rows} AS t(build_id, gold_table, built_at_utc, row_count, question, grain, scope, sources, "
        f"checks_pass, checks_fail, checks_warn, checks_info)")

    if args.no_snapshot:
        print("snapshot skipped (--no-snapshot)")
    elif status_counts.get("FAIL"):
        print("snapshot not exported because checks failed")
    else:
        keys_by_table = {t["name"]: t["keys"] for t in TABLES}
        keys_by_table[LOG_TABLE] = ["gold_table"]
        snapshot_results = export_snapshot(warehouse, build_id, built_at, keys_by_table)
        results.extend(snapshot_results)
        for record in snapshot_results:
            status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1
        print(f"snapshot written to {SNAPSHOT.relative_to(ROOT).as_posix()}/ ({len(snapshot_results)} tables)")

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    QUALITY_AUDIT.parent.mkdir(exist_ok=True)
    with open(QUALITY_AUDIT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["table", "check", "category", "severity", "expected", "observed", "status",
                                               "detail", "build_id", "checked_at"])
        writer.writeheader()
        for record in results:
            writer.writerow({**record, "build_id": build_id, "checked_at": checked_at})
    with open(LINEAGE_AUDIT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["gold_table", "source", "source_sha256", "row_count", "scope", "build_id",
                                               "built_at_utc"])
        writer.writeheader()
        for t in TABLES:
            for source in t["sources"]:
                name = source.split("/", 1)[1] if source.startswith("outputs/") else ""
                writer.writerow({"gold_table": f"{G}.{t['name']}", "source": source,
                                 "source_sha256": local_hashes.get(name, ""), "row_count": counts[t["name"]],
                                 "scope": t["scope"], "build_id": build_id, "built_at_utc": built_at})

    print("\ngold tables:")
    for t in TABLES:
        print(f"  {t['name']:<26} {counts[t['name']]:>8,} rows  [{t['question']}]")
    print(f"\nchecks: {status_counts}")
    for record in results:
        if record["status"] != "PASS":
            print(f"  {record['status']}: {record['table']}: {record['check']}: observed {record['observed']} "
                  f"(expected {record['expected']}) {record['detail']}")
    print(f"audit written to {QUALITY_AUDIT.relative_to(ROOT).as_posix()} and {LINEAGE_AUDIT.relative_to(ROOT).as_posix()}")
    return 1 if status_counts.get("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
