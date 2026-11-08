"""Build and validate the Silver layer (workspace.ante_silver) from verified Bronze tables.

Every table is rebuilt with CREATE OR REPLACE, so reruns are idempotent. Bronze is read only.
Uses the same CLI-authenticated SQL Statement Execution client as scripts/load-bronze.py.

usage: python scripts/build-silver.py [--profile ante] [--checks-only] [--workers 4]
"""
import argparse
import csv
import importlib.util
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("load_bronze", ROOT / "scripts" / "load-bronze.py")
bronze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bronze)

B = "workspace.ante_bronze"
S = "workspace.ante_silver"
QUALITY_AUDIT = ROOT / "audit" / "silver-quality-audit.csv"
NULL_PROFILE = ROOT / "audit" / "silver-null-profile.csv"
BRONZE_AUDIT = ROOT / "audit" / "bronze-load-audit.csv"
LINEAGE_DROP = "source_file, ingested_at"
FAR_FUTURE = "TIMESTAMP_NTZ'9999-12-31 00:00:00'"


#=== table definitions ===

TABLES = []


def table(name, grain, keys, sql, origin="synthetic", player_level=False, transform="none (bronze columns as-is)"):
    TABLES.append({"name": name, "grain": grain, "keys": keys, "sql": sql, "origin": origin,
                   "player_level": player_level, "transform": transform})


def passthrough(name, drop=""):
    extra = f", {drop}" if drop else ""
    return f"SELECT * EXCEPT ({LINEAGE_DROP}{extra}), source_file AS bronze_source_file FROM {B}.{name}"


def with_gaming_date(name, ts_column, out_column, extra_columns=""):
    #half-open [start, end) match, the same rule as the analysis scripts; calendar days do not overlap
    return (f"SELECT t.* EXCEPT ({LINEAGE_DROP}), c.gaming_date AS {out_column}{extra_columns}, "
            f"t.source_file AS bronze_source_file FROM {B}.{name} t LEFT JOIN {S}.calendar_day c "
            f"ON t.{ts_column} >= c.gaming_day_start_utc AND t.{ts_column} < c.gaming_day_end_utc")


#reference and calendar
table("calendar_day", "gaming date", ["gaming_date"], passthrough("calendar_day"))
table("floor_locations", "floor node (zone, bank, slot, outlet, entrance)", ["location_id"],
      f"SELECT f.* EXCEPT ({LINEAGE_DROP}), z.name AS zone_name, f.source_file AS bronze_source_file "
      f"FROM {B}.floor_locations f LEFT JOIN (SELECT zone_id, name FROM {B}.floor_locations WHERE node_type = 'zone') z "
      f"ON f.zone_id = z.zone_id",
      transform="adds zone_name from the zone node of the same zone_id (one zone node per zone, checked)")
table("floor_walkways", "walkway between zone hubs", ["walkway_id"], passthrough("floor_walkways"))
table("machines", "machine", ["machine_id"], passthrough("machines"))
table("game_math", "game configuration", ["config_id"], passthrough("game_math"))
table("placements", "machine x location/config interval", ["placement_id"],
      f"SELECT p.* EXCEPT ({LINEAGE_DROP}), cf.gaming_date AS effective_from_gaming_date, "
      f"cl.gaming_date AS last_gaming_date, p.source_file AS bronze_source_file FROM {B}.placements p "
      f"LEFT JOIN {S}.calendar_day cf ON p.effective_from_utc = cf.gaming_day_start_utc "
      f"LEFT JOIN {S}.calendar_day cl ON p.effective_to_utc - INTERVAL 1 SECOND >= cl.gaming_day_start_utc "
      f"AND p.effective_to_utc - INTERVAL 1 SECOND < cl.gaming_day_end_utc",
      transform="adds effective_from_gaming_date (start is a gaming-day boundary) and last_gaming_date "
                "(inclusive day containing effective_to - 1s); null when outside the window or still placed")
table("cost_rules", "supplied cost rule", ["cost_rule_id"],
      f"SELECT * EXCEPT ({LINEAGE_DROP}), CAST(rate AS DECIMAL(18,6)) AS rate_value, source_file AS bronze_source_file "
      f"FROM {B}.cost_rules",
      transform="adds rate_value: exact decimal cast of the text rate (original rate kept)")
table("action_catalog", "budget action type", ["action_type_id"], passthrough("action_catalog"))
table("events", "calendar event", ["event_id"], passthrough("events"))

#machine facts
table("machine_day", "machine x gaming date (all play, incl. off-floor rows)", ["machine_id", "gaming_date"],
      passthrough("machine_day"))
table("prepared_daily_view", "machine x gaming date, joined in dollars", ["machine_id", "gaming_date"],
      passthrough("prepared_daily_view", drop="_rescued_data"),
      transform="drops _rescued_data (an artifact of read_files; verified null on every row)")
table("property_day", "gaming date (whole property)", ["gaming_date"], passthrough("property_day"))
table("machine_hour", "machine x hour while installed", ["machine_id", "hour_start_utc"], passthrough("machine_hour"))
table("operations", "operations ticket", ["operation_id"],
      with_gaming_date("operations", "opened_at_utc", "opened_gaming_date",
                       ", timestampdiff(SECOND, t.opened_at_utc, t.available_at_utc) AS unavailable_seconds, "
                       "t.available_at_utc IS NULL AS unresolved"),
      transform="adds opened_gaming_date, unavailable_seconds (available - opened; null if unresolved), unresolved flag")
table("zone_traffic_hourly", "sensor x hour", ["sensor_id", "hour_start_utc"],
      with_gaming_date("zone_traffic_hourly", "hour_start_utc", "gaming_date"),
      transform="adds gaming_date of the hour start; entries_count left null when offline (never zero-filled)")
table("security_incidents", "reported incident", ["incident_id"],
      with_gaming_date("security_incidents", "occurred_at_utc", "occurred_gaming_date"),
      transform="adds occurred_gaming_date")
table("shift_roster", "staff assignment", ["assignment_id"], passthrough("shift_roster"))

#guests (player-level; stays inside databricks)
table("players", "loyalty account", ["player_id"], passthrough("players"), player_level=True)
table("sessions", "carded session segment", ["session_segment_id"], passthrough("sessions"), player_level=True)
table("trips", "observed property visit", ["observed_visit_id"],
      with_gaming_date("trips", "first_observed_at_utc", "first_gaming_date"), player_level=True,
      transform="adds first_gaming_date")
table("loyalty_ledger", "points award or redemption", ["loyalty_event_id"],
      with_gaming_date("loyalty_ledger", "event_at_utc", "event_gaming_date",
                       ", c.gaming_date IS NOT NULL AS in_analysis_window"), player_level=True,
      transform="adds event_gaming_date and in_analysis_window; out-of-window events kept, not dropped")
table("fnb_transactions", "POS ticket (carded and uncarded)", ["transaction_id"],
      with_gaming_date("fnb_transactions", "event_at_utc", "gaming_date", ", t.comp_id IS NOT NULL AS is_comp"),
      player_level=True, transform="adds gaming_date and is_comp (comp_id present); comp cogs remain inside cogs_cents")
table("player_period", "tracked player x month", ["player_id", "period_month"], passthrough("player_period"),
      player_level=True)
table("visitor_profile_monthly", "fictional survey metric x month", ["month", "survey_metric"],
      passthrough("visitor_profile_monthly"))

#real public context, never mixed with synthetic rows
for name, grain, keys in [
        ("real_weather_daily", "station x calendar date (not gaming date)", ["station_id", "source_date"]),
        ("real_city_visitation_monthly", "month x geography", ["month", "geography"]),
        ("real_visitor_profile_summary", "survey metric", ["metric_id", "segment_group", "segment"]),
        ("real_visitor_profile_segments", "survey metric x segment", ["metric_id", "segment_group", "segment"]),
        ("real_benchmark_public", "missouri casino x slot category x month", ["property_id", "category", "month"]),
        ("real_benchmark_property_monthly", "missouri casino x month", ["property_id", "month"]),
        ("real_benchmark_state_monthly", "month", ["month"])]:
    table(name, grain, keys, passthrough(name), origin="real_public")


def create_sql(spec):
    comment = f"{spec['grain']}; keys: {', '.join(spec['keys'])}".replace("'", "")
    properties = {"ante.layer": "silver", "ante.data_origin": spec["origin"],
                  "ante.contains_player_level": str(spec["player_level"]).lower(), "ante.grain": spec["grain"]}
    props = ", ".join(f"'{k}' = '{v}'" for k, v in properties.items())
    return f"CREATE OR REPLACE TABLE {S}.{spec['name']} COMMENT '{comment}' TBLPROPERTIES ({props}) AS {spec['sql']}"


#=== quality checks ===

CHECKS = []


def check(table_name, name, category, sql, expected=0, severity="error"):
    #sql returns one row with column observed (and optional detail); pass when observed == expected
    CHECKS.append({"table": table_name, "check": name, "category": category, "sql": sql, "expected": expected,
                   "severity": severity})


def count_where(table_name, condition):
    return f"SELECT COUNT(*) AS observed FROM {S}.{table_name} WHERE {condition}"


def add_generic_checks(bronze_counts):
    for spec in TABLES:
        name, keys = spec["name"], spec["keys"]
        key_list = ", ".join(keys)
        severity = "warn" if spec["origin"] == "real_public" else "error"
        check(name, "row count equals bronze", "row count",
              f"SELECT COUNT(*) AS observed FROM {S}.{name}", expected=bronze_counts[name])
        check(name, f"key ({key_list}) unique", "keys",
              f"SELECT COUNT(*) - COUNT(DISTINCT {key_list}) AS observed FROM {S}.{name}", severity=severity)
        check(name, f"key ({key_list}) not null", "keys",
              count_where(name, " OR ".join(f"{k} IS NULL" for k in keys)), severity=severity)
        check(name, "no exact duplicate rows", "duplicates",
              f"SELECT COUNT(*) - (SELECT COUNT(*) FROM (SELECT DISTINCT * EXCEPT (bronze_source_file) FROM {S}.{name})) "
              f"AS observed FROM {S}.{name}")
    check("*", "bronze columns preserved with identical types", "schema",
          f"SELECT COUNT(*) AS observed, concat_ws('; ', slice(collect_list(concat(b.table_name, '.', b.column_name)), 1, 10)) "
          f"AS detail FROM workspace.information_schema.columns b LEFT JOIN workspace.information_schema.columns s "
          f"ON s.table_schema = 'ante_silver' AND s.table_name = b.table_name AND s.column_name = b.column_name "
          f"WHERE b.table_schema = 'ante_bronze' AND b.column_name NOT IN ('source_file', 'ingested_at', '_rescued_data') "
          f"AND (s.column_name IS NULL OR s.full_data_type != b.full_data_type)")
    check("*", "silver table count", "schema",
          f"SELECT COUNT(*) AS observed FROM workspace.information_schema.tables WHERE table_schema = 'ante_silver'",
          expected=len(TABLES))
    check("*", "real public tables isolated under real_ prefix", "data origin",
          f"SELECT COUNT(*) AS observed FROM workspace.information_schema.tables "
          f"WHERE table_schema = 'ante_silver' AND table_name LIKE 'real\\_%'", expected=7)


def add_business_checks():
    #calendar
    check("calendar_day", "181 gaming dates, 2025-01-01 to 2025-06-30", "date range",
          f"SELECT CAST(COUNT(*) = 181 AND MIN(gaming_date) = DATE'2025-01-01' AND MAX(gaming_date) = DATE'2025-06-30' "
          f"AS INT) AS observed FROM {S}.calendar_day", expected=1)
    check("calendar_day", "days contiguous and non-overlapping", "invariant",
          f"SELECT COUNT(*) AS observed FROM (SELECT gaming_day_end_utc, LEAD(gaming_day_start_utc) OVER "
          f"(ORDER BY gaming_date) AS next_start FROM {S}.calendar_day) WHERE next_start IS NOT NULL "
          f"AND next_start != gaming_day_end_utc")
    check("calendar_day", "scheduled_seconds = end - start", "invariant",
          count_where("calendar_day", "scheduled_seconds != timestampdiff(SECOND, gaming_day_start_utc, gaming_day_end_utc)"))
    check("calendar_day", "dst gaming day 2025-03-08 has 82,800 seconds", "invariant",
          f"SELECT MIN(scheduled_seconds) AS observed FROM {S}.calendar_day WHERE gaming_date = DATE'2025-03-08'",
          expected=82800)

    #floor and machines
    check("floor_locations", "one zone node per zone_id", "keys",
          f"SELECT COUNT(*) - COUNT(DISTINCT zone_id) AS observed FROM {S}.floor_locations WHERE node_type = 'zone'")
    check("floor_locations", "parent_location_id exists", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.floor_locations c LEFT JOIN {S}.floor_locations p "
          f"ON c.parent_location_id = p.location_id WHERE c.parent_location_id IS NOT NULL AND p.location_id IS NULL")
    check("floor_locations", "every node has a zone_name", "referential integrity", count_where("floor_locations", "zone_name IS NULL"))
    check("machines", "install_date <= retire_date", "invariant", count_where("machines", "retire_date < install_date"))
    check("game_math", "configured_rtp between 0 and 1", "invariant",
          count_where("game_math", "configured_rtp <= 0 OR configured_rtp >= 1"))

    #placements
    for target, column in [("machines", "machine_id"), ("game_math", "config_id")]:
        check("placements", f"{column} exists in {target}", "referential integrity",
              f"SELECT COUNT(*) AS observed FROM {S}.placements p LEFT ANTI JOIN {S}.{target} t USING ({column})")
    check("placements", "location_id is a slot", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.placements p LEFT ANTI JOIN "
          f"(SELECT location_id FROM {S}.floor_locations WHERE node_type = 'slot') f USING (location_id)")
    check("placements", "effective_from < effective_to", "invariant",
          count_where("placements", "effective_to_utc IS NOT NULL AND effective_to_utc <= effective_from_utc"))
    for column in ["machine_id", "location_id"]:
        check("placements", f"no overlapping intervals per {column}", "many-to-many guard",
              f"SELECT COUNT(*) AS observed FROM {S}.placements a JOIN {S}.placements b ON a.{column} = b.{column} "
              f"AND a.placement_id < b.placement_id AND a.effective_from_utc < coalesce(b.effective_to_utc, {FAR_FUTURE}) "
              f"AND b.effective_from_utc < coalesce(a.effective_to_utc, {FAR_FUTURE})")
    check("placements", "in-window starts fall on a gaming-day boundary", "invariant",
          f"SELECT COUNT(*) AS observed FROM {S}.placements WHERE effective_from_gaming_date IS NULL "
          f"AND effective_from_utc >= (SELECT MIN(gaming_day_start_utc) FROM {S}.calendar_day) "
          f"AND effective_from_utc < (SELECT MAX(gaming_day_end_utc) FROM {S}.calendar_day)")

    #machine_day
    for target, column in [("machines", "machine_id"), ("game_math", "config_id"), ("calendar_day", "gaming_date")]:
        check("machine_day", f"{column} exists in {target}", "referential integrity",
              f"SELECT COUNT(*) AS observed FROM {S}.machine_day m LEFT ANTI JOIN {S}.{target} t USING ({column}) "
              f"WHERE m.{column} IS NOT NULL")
    check("machine_day", "date window: 181 dates x 310 machines", "date range",
          f"SELECT CAST(COUNT(DISTINCT gaming_date) = 181 AND COUNT(DISTINCT machine_id) = 310 AND "
          f"MIN(gaming_date) = DATE'2025-01-01' AND MAX(gaming_date) = DATE'2025-06-30' AS INT) AS observed "
          f"FROM {S}.machine_day", expected=1)
    check("machine_day", "house_win = coin_in - payout (integer cents)", "money invariant",
          count_where("machine_day", "house_win_cents IS DISTINCT FROM coin_in_cents - payout_cents"))
    check("machine_day", "theo = coin_in x (1 - configured rtp)", "theo vs actual",
          f"SELECT COUNT(*) AS observed FROM {S}.machine_day m JOIN {S}.game_math g USING (config_id) "
          f"WHERE abs(m.theo_cents - m.coin_in_cents * (1 - g.configured_rtp)) > 0.0001")
    check("machine_day", "carded subsets <= totals", "invariant",
          count_where("machine_day", "carded_coin_in_cents > coin_in_cents OR carded_spins > paid_spins "
                                     "OR carded_payout_cents > payout_cents OR payout_positive_spins > paid_spins"))
    check("machine_day", "available <= scheduled seconds", "invariant",
          count_where("machine_day", "available_seconds > scheduled_seconds OR available_seconds < 0"))
    check("machine_day", "occupied <= available seconds", "invariant",
          count_where("machine_day", "occupied_seconds > available_seconds"), severity="warn")
    check("machine_day", "no play recorded on off-floor rows", "invariant",
          count_where("machine_day", "NOT active AND (paid_spins > 0 OR coin_in_cents != 0)"))
    check("machine_day", "active rows have a location", "referential integrity",
          count_where("machine_day", "active AND location_id IS NULL"), severity="warn")
    check("machine_day", "location_id matches the placement covering the day start", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.machine_day m JOIN {S}.calendar_day c USING (gaming_date) "
          f"WHERE m.location_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {S}.placements p WHERE p.machine_id = m.machine_id "
          f"AND p.location_id = m.location_id AND p.effective_from_utc <= c.gaming_day_start_utc "
          f"AND c.gaming_day_start_utc < coalesce(p.effective_to_utc, {FAR_FUTURE}))")

    #prepared view and property day agree with the base facts
    check("prepared_daily_view", "dollar columns equal machine_day cents", "money invariant",
          f"SELECT COUNT(*) AS observed FROM {S}.prepared_daily_view p FULL JOIN {S}.machine_day m "
          f"ON p.machine_id = m.machine_id AND p.gaming_date = m.gaming_date WHERE p.machine_id IS NULL OR m.machine_id IS NULL "
          f"OR p.coin_in_usd * 100 != m.coin_in_cents OR p.payout_usd * 100 != m.payout_cents "
          f"OR p.house_win_usd * 100 != m.house_win_cents OR abs(p.theo_usd * 100 - m.theo_cents) > 0.005 "
          f"OR p.paid_spins != m.paid_spins OR p.active != m.active OR NOT (p.location_id <=> m.location_id)")
    check("prepared_daily_view", "accounting identities (house win, luck, contribution)", "money invariant",
          count_where("prepared_daily_view",
                      "house_win_usd != coin_in_usd - payout_usd OR luck_residual_usd != house_win_usd - theo_usd "
                      "OR contribution_after_supplied_costs_usd != house_win_usd - lease_cost_usd - service_cost_usd "
                      "- power_cost_usd - repair_cost_usd"))
    check("prepared_daily_view", "1,018 off-floor rows (null location)", "invariant",
          count_where("prepared_daily_view", "location_id IS NULL"), expected=1018)
    check("prepared_daily_view", "model-safe 7-day lag uses only prior days", "leakage guard",
          f"SELECT COUNT(*) AS observed FROM (SELECT lag_coin_in_7d_mean_usd, AVG(coin_in_usd) OVER (PARTITION BY machine_id "
          f"ORDER BY gaming_date ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING) AS expected FROM {S}.prepared_daily_view) "
          f"WHERE (expected IS NULL) != (lag_coin_in_7d_mean_usd IS NULL) OR abs(expected - lag_coin_in_7d_mean_usd) > 0.000001")
    check("property_day", "daily totals equal the sum of machine-days", "money invariant",
          f"SELECT COUNT(*) AS observed FROM {S}.property_day d FULL JOIN (SELECT gaming_date, SUM(coin_in_usd) AS coin, "
          f"SUM(payout_usd) AS payout, SUM(theo_usd) AS theo, SUM(available_hours) AS hours, COUNT_IF(active) AS machines "
          f"FROM {S}.prepared_daily_view GROUP BY gaming_date) m USING (gaming_date) WHERE d.coin_in_usd IS DISTINCT FROM m.coin "
          f"OR d.payout_usd IS DISTINCT FROM m.payout OR abs(d.theo_usd - m.theo) > 0.0001 "
          f"OR abs(d.available_machine_hours - m.hours) > 0.000001 OR d.active_machines != m.machines")
    check("property_day", "house win and contribution identities", "money invariant",
          count_where("property_day", "house_win_usd != coin_in_usd - payout_usd OR contribution_after_supplied_costs_usd "
                                      "!= house_win_usd + pos_cash_usd - pos_cogs_usd - machine_supplied_costs_usd - tech_labor_cost_usd"))
    check("property_day", "comp cost is inside pos cogs (never above it)", "comp double count guard",
          count_where("property_day", "comp_cost_usd > pos_cogs_usd"))
    check("property_day", "pos cash, cogs and comps equal fnb tickets by gaming date", "money invariant",
          f"SELECT COUNT(*) AS observed FROM {S}.property_day d FULL JOIN (SELECT gaming_date, SUM(cash_receipt_cents) AS cash, "
          f"SUM(cogs_cents) AS cogs, SUM(IF(is_comp, cogs_cents, 0)) AS comp_cost, SUM(IF(is_comp, face_value_cents, 0)) AS comp_face "
          f"FROM {S}.fnb_transactions WHERE gaming_date IS NOT NULL GROUP BY gaming_date) f USING (gaming_date) "
          f"WHERE d.pos_cash_usd * 100 IS DISTINCT FROM f.cash OR d.pos_cogs_usd * 100 IS DISTINCT FROM f.cogs "
          f"OR d.comp_cost_usd * 100 IS DISTINCT FROM f.comp_cost OR d.comp_face_value_usd * 100 IS DISTINCT FROM f.comp_face")
    check("property_day", "faults_opened equals fault tickets by opened gaming date", "invariant",
          f"SELECT COUNT(*) AS observed FROM {S}.property_day d LEFT JOIN (SELECT opened_gaming_date AS gaming_date, COUNT(*) AS n "
          f"FROM {S}.operations WHERE event_type = 'fault_repair' GROUP BY 1) o USING (gaming_date) "
          f"WHERE d.faults_opened != coalesce(o.n, 0)", severity="warn")

    #machine_hour rolls up to machine_day
    check("machine_hour", "hourly meters sum to machine_day", "money invariant",
          f"SELECT COUNT(*) AS observed FROM {S}.machine_day m LEFT JOIN (SELECT machine_id, gaming_date, SUM(coin_in_cents) AS coin, "
          f"SUM(payout_cents) AS payout, SUM(paid_spins) AS spins, SUM(payout_positive_spins) AS hits, "
          f"SUM(available_seconds) AS avail, SUM(occupied_seconds) AS occ, SUM(carded_coin_in_cents) AS carded "
          f"FROM {S}.machine_hour GROUP BY ALL) h USING (machine_id, gaming_date) WHERE m.coin_in_cents != coalesce(h.coin, 0) "
          f"OR m.payout_cents != coalesce(h.payout, 0) OR m.paid_spins != coalesce(h.spins, 0) "
          f"OR m.payout_positive_spins != coalesce(h.hits, 0) OR m.available_seconds != coalesce(h.avail, 0) "
          f"OR m.occupied_seconds != coalesce(h.occ, 0) OR m.carded_coin_in_cents != coalesce(h.carded, 0)")
    check("machine_hour", "machine_id exists in machines", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.machine_hour LEFT ANTI JOIN {S}.machines USING (machine_id)")

    #operations
    check("operations", "machine_id exists in machines", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.operations LEFT ANTI JOIN {S}.machines USING (machine_id)")
    check("operations", "location_id exists in floor_locations", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.operations o LEFT ANTI JOIN {S}.floor_locations USING (location_id) "
          f"WHERE o.location_id IS NOT NULL")
    check("operations", "event_type in documented set", "invariant",
          count_where("operations", "event_type NOT IN ('fault_repair', 'scheduled_maintenance', 'move', 'install', 'retire')"))
    check("operations", "available_at >= opened_at", "invariant",
          count_where("operations", "available_at_utc < opened_at_utc"))
    check("operations", "work starts inside the outage", "invariant",
          count_where("operations", "work_started_at_utc < opened_at_utc OR work_started_at_utc > available_at_utc"),
          severity="warn")
    check("operations", "opened inside the analysis window", "date range",
          count_where("operations", "opened_gaming_date IS NULL"), severity="warn")
    check("operations", "unresolved tickets (no available_at)", "review", count_where("operations", "unresolved"),
          severity="info")

    #guests
    check("players", "observed_through after enrollment", "invariant",
          count_where("players", "observed_through_utc < enrolled_at_utc"), severity="warn")
    for target, column in [("players", "player_id"), ("machines", "machine_id"), ("game_math", "config_id"),
                           ("trips", "observed_visit_id"), ("calendar_day", "gaming_date")]:
        check("sessions", f"{column} exists in {target}", "referential integrity",
              f"SELECT COUNT(*) AS observed FROM {S}.sessions s LEFT ANTI JOIN {S}.{target} t USING ({column}) "
              f"WHERE s.{column} IS NOT NULL")
    check("sessions", "session player matches its visit's player", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.sessions s JOIN {S}.trips t USING (observed_visit_id) "
          f"WHERE s.player_id != t.player_id")
    check("sessions", "gaming_date matches the start time", "invariant",
          f"SELECT COUNT(*) AS observed FROM {S}.sessions s JOIN {S}.calendar_day c USING (gaming_date) "
          f"WHERE s.start_utc < c.gaming_day_start_utc OR s.start_utc >= c.gaming_day_end_utc")
    check("sessions", "segment ends by the gaming-day end (split at 06:00)", "invariant",
          f"SELECT COUNT(*) AS observed FROM {S}.sessions s JOIN {S}.calendar_day c USING (gaming_date) "
          f"WHERE s.end_utc > c.gaming_day_end_utc OR s.end_utc < s.start_utc")
    check("sessions", "coin_in = fixed bet x paid spins", "money invariant",
          count_where("sessions", "coin_in_cents != fixed_bet_cents * paid_spins OR payout_positive_spins > paid_spins"))
    check("sessions", "theo = coin_in x (1 - configured rtp)", "theo vs actual",
          f"SELECT COUNT(*) AS observed FROM {S}.sessions s JOIN {S}.game_math g USING (config_id) "
          f"WHERE abs(s.theo_cents - s.coin_in_cents * (1 - g.configured_rtp)) > 0.0001")
    check("sessions", "carded totals by machine-day equal machine_day carded meters", "money invariant",
          f"SELECT COUNT(*) AS observed FROM {S}.machine_day m FULL JOIN (SELECT machine_id, gaming_date, SUM(coin_in_cents) AS coin, "
          f"SUM(payout_cents) AS payout, SUM(paid_spins) AS spins, SUM(theo_cents) AS theo FROM {S}.sessions GROUP BY ALL) s "
          f"ON m.machine_id = s.machine_id AND m.gaming_date = s.gaming_date WHERE m.machine_id IS NULL "
          f"OR m.carded_coin_in_cents != coalesce(s.coin, 0) OR m.carded_payout_cents != coalesce(s.payout, 0) "
          f"OR m.carded_spins != coalesce(s.spins, 0) OR abs(m.carded_theo_cents - coalesce(s.theo, 0)) > 0.0001")
    check("trips", "player_id exists in players", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.trips LEFT ANTI JOIN {S}.players USING (player_id)")
    check("trips", "first_observed <= last_observed", "invariant",
          count_where("trips", "last_observed_at_utc < first_observed_at_utc"))
    check("player_period", "tracked money identities", "money invariant",
          count_where("player_period", "tracked_net_usd != tracked_payout_usd - tracked_coin_in_usd "
                                       "OR luck_residual_usd != tracked_net_usd + theo_usd "
                                       "OR tracked_contribution_usd != -tracked_net_usd + linked_pos_cash_usd - linked_pos_cogs_usd"))
    check("player_period", "comp cost is inside linked pos cogs", "comp double count guard",
          count_where("player_period", "comp_cost_usd > linked_pos_cogs_usd"))
    check("player_period", "coin-in, payout and theo equal sessions by player and gaming month", "money invariant",
          f"SELECT COUNT(*) AS observed FROM {S}.player_period p FULL JOIN (SELECT player_id, date_format(gaming_date, 'yyyy-MM') "
          f"AS period_month, SUM(coin_in_cents) AS coin, SUM(payout_cents) AS payout, SUM(theo_cents) AS theo "
          f"FROM {S}.sessions GROUP BY ALL) s USING (player_id, period_month) "
          f"WHERE coalesce(p.tracked_coin_in_usd * 100, 0) != coalesce(s.coin, 0) "
          f"OR coalesce(p.tracked_payout_usd * 100, 0) != coalesce(s.payout, 0) "
          f"OR abs(coalesce(p.theo_usd * 100, 0) - coalesce(s.theo, 0)) > 0.005")
    check("player_period", "player_id exists in players", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.player_period LEFT ANTI JOIN {S}.players USING (player_id)")

    #loyalty and f&b: one comp, one ticket, one cost
    check("loyalty_ledger", "player_id exists in players", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.loyalty_ledger LEFT ANTI JOIN {S}.players USING (player_id)")
    check("loyalty_ledger", "event_type in award/redeem", "invariant",
          count_where("loyalty_ledger", "event_type NOT IN ('award', 'redeem')"))
    check("loyalty_ledger", "awards add points, redemptions spend points", "invariant",
          count_where("loyalty_ledger", "(event_type = 'award' AND points_delta <= 0) OR (event_type = 'redeem' AND points_delta >= 0)"))
    check("loyalty_ledger", "award source segment exists in sessions", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.loyalty_ledger l LEFT ANTI JOIN {S}.sessions s "
          f"ON l.source_segment_id = s.session_segment_id WHERE l.event_type = 'award'")
    check("loyalty_ledger", "each redemption links to exactly one fnb ticket", "many-to-many guard",
          f"SELECT COUNT(*) AS observed FROM (SELECT l.loyalty_event_id, COUNT(f.transaction_id) AS n FROM {S}.loyalty_ledger l "
          f"LEFT JOIN {S}.fnb_transactions f ON l.redeemed_pos_id = f.transaction_id WHERE l.event_type = 'redeem' GROUP BY 1) "
          f"WHERE n != 1")
    check("loyalty_ledger", "redemption economic cost equals the linked ticket cogs", "comp double count guard",
          f"SELECT COUNT(*) AS observed FROM {S}.loyalty_ledger l JOIN {S}.fnb_transactions f ON l.redeemed_pos_id = f.transaction_id "
          f"WHERE l.economic_cost_cents IS DISTINCT FROM f.cogs_cents OR l.face_value_cents IS DISTINCT FROM f.face_value_cents "
          f"OR l.player_id IS DISTINCT FROM f.player_id")
    check("loyalty_ledger", "events after the window end kept and flagged", "review",
          count_where("loyalty_ledger", "NOT in_analysis_window"), severity="info")
    check("fnb_transactions", "each comp ticket has exactly one redemption", "many-to-many guard",
          f"SELECT COUNT(*) AS observed FROM (SELECT f.transaction_id, COUNT(l.loyalty_event_id) AS n FROM {S}.fnb_transactions f "
          f"LEFT JOIN {S}.loyalty_ledger l ON l.redeemed_pos_id = f.transaction_id AND l.event_type = 'redeem' "
          f"WHERE f.is_comp GROUP BY 1) WHERE n != 1")
    check("fnb_transactions", "comp_id is the redemption that paid for it", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.fnb_transactions f LEFT JOIN {S}.loyalty_ledger l "
          f"ON l.loyalty_event_id = f.comp_id WHERE f.is_comp AND (l.loyalty_event_id IS NULL "
          f"OR l.redeemed_pos_id IS DISTINCT FROM f.transaction_id)")
    check("fnb_transactions", "comps carry no cash receipt", "comp double count guard",
          count_where("fnb_transactions", "is_comp AND cash_receipt_cents != 0"))
    check("fnb_transactions", "non-negative money", "invariant",
          count_where("fnb_transactions", "cash_receipt_cents < 0 OR cogs_cents < 0 OR face_value_cents < 0"))
    check("fnb_transactions", "outlet_id exists in floor_locations", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.fnb_transactions f LEFT ANTI JOIN "
          f"(SELECT outlet_id FROM {S}.floor_locations WHERE node_type = 'outlet') o USING (outlet_id)")
    check("fnb_transactions", "carded player_id exists in players", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.fnb_transactions f LEFT ANTI JOIN {S}.players USING (player_id) "
          f"WHERE f.player_id IS NOT NULL")
    check("fnb_transactions", "visit exists and belongs to the same player", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.fnb_transactions f LEFT JOIN {S}.trips t USING (observed_visit_id) "
          f"WHERE f.observed_visit_id IS NOT NULL AND (t.observed_visit_id IS NULL OR t.player_id != f.player_id)")
    check("fnb_transactions", "comp cost total equals redemption economic cost total (same money, count once)",
          "comp double count guard",
          f"SELECT (SELECT SUM(cogs_cents) FROM {S}.fnb_transactions WHERE is_comp) - "
          f"(SELECT SUM(economic_cost_cents) FROM {S}.loyalty_ledger WHERE event_type = 'redeem') AS observed")
    check("fnb_transactions", "tickets outside the window kept and flagged", "review",
          count_where("fnb_transactions", "gaming_date IS NULL"), severity="info")

    #context
    check("zone_traffic_hourly", "entries null exactly when the counter is offline", "missing is not zero",
          count_where("zone_traffic_hourly", "(entries_count IS NULL) != (counter_status = 'offline')"))
    check("zone_traffic_hourly", "coverage_minutes between 0 and 60", "invariant",
          count_where("zone_traffic_hourly", "coverage_minutes < 0 OR coverage_minutes > 60"))
    check("zone_traffic_hourly", "zone_id is a zone", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.zone_traffic_hourly LEFT ANTI JOIN "
          f"(SELECT zone_id FROM {S}.floor_locations WHERE node_type = 'zone') z USING (zone_id)")
    check("zone_traffic_hourly", "hours inside the analysis window", "date range",
          count_where("zone_traffic_hourly", "gaming_date IS NULL"), severity="warn")
    check("events", "start <= end and announced <= start", "invariant",
          count_where("events", "end_date < start_date OR announced_date > start_date"), severity="warn")
    check("security_incidents", "zone and machine exist", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.security_incidents i LEFT JOIN (SELECT zone_id FROM {S}.floor_locations "
          f"WHERE node_type = 'zone') z USING (zone_id) LEFT JOIN {S}.machines m USING (machine_id) "
          f"WHERE z.zone_id IS NULL OR (i.machine_id IS NOT NULL AND m.machine_id IS NULL)")
    check("security_incidents", "severity 1-3 and reported after occurrence", "invariant",
          count_where("security_incidents", "severity NOT BETWEEN 1 AND 3 OR reported_at_utc < occurred_at_utc"), severity="warn")
    check("shift_roster", "gaming_date and zone exist", "referential integrity",
          f"SELECT COUNT(*) AS observed FROM {S}.shift_roster r LEFT JOIN {S}.calendar_day c USING (gaming_date) "
          f"LEFT JOIN (SELECT zone_id FROM {S}.floor_locations WHERE node_type = 'zone') z USING (zone_id) "
          f"WHERE c.gaming_date IS NULL OR (r.zone_id IS NOT NULL AND z.zone_id IS NULL)")
    check("shift_roster", "end after start and non-negative headcount", "invariant",
          count_where("shift_roster", "end_utc <= start_utc OR planned_headcount < 0 OR actual_headcount < 0"))
    check("cost_rules", "rate parses as a decimal", "invariant", count_where("cost_rules", "rate_value IS NULL"))
    check("action_catalog", "guide lease example costs $3,380", "invariant",
          f"SELECT CAST((unit_cost_cents + recurring_cost_cents_per_day * GREATEST(0, 30 - lead_days)) / 100 AS INT) AS observed "
          f"FROM {S}.action_catalog WHERE action_type_id = 'A-REPLACE-LEASE'", expected=3380)
    for name in [t["name"] for t in TABLES if t["origin"] == "real_public"]:
        check(name, "every row carries source url and sha-256 provenance", "data origin",
              count_where(name, "source_url IS NULL OR source_sha256 IS NULL"))

    #anomalies kept visible for review (preserved, not corrected)
    check("machine_day", "machines with hit rate |z| > 4 vs published hit probability", "review",
          f"SELECT COUNT(*) AS observed, concat_ws('; ', collect_list(machine_id || ' z=' || CAST(ROUND(z, 1) AS STRING))) AS detail "
          f"FROM (SELECT m.machine_id, (SUM(m.payout_positive_spins) / SUM(m.paid_spins) - MAX(g.hit_probability)) / "
          f"SQRT(MAX(g.hit_probability) * (1 - MAX(g.hit_probability)) / SUM(m.paid_spins)) AS z FROM {S}.machine_day m "
          f"JOIN {S}.game_math g USING (config_id) WHERE m.paid_spins > 0 GROUP BY m.machine_id) WHERE abs(z) > 4",
          severity="info")
    check("machine_day", "machine-days with negative house win (valid, preserved)", "review",
          count_where("machine_day", "house_win_cents < 0"), severity="info")
    check("player_period", "player-months with a net loss (negative tracked net, preserved)", "review",
          count_where("player_period", "tracked_net_usd < 0"), severity="info")
    check("zone_traffic_hourly", "offline counter hours (null, not zero)", "review",
          count_where("zone_traffic_hourly", "entries_count IS NULL"), severity="info")


#=== run ===

def run_check(warehouse, spec):
    record = {k: spec[k] for k in ("table", "check", "category", "severity", "expected")}
    try:
        row = warehouse.execute(spec["sql"])[0]
        observed = row.get("observed")
        observed = int(observed) if observed is not None and str(observed).lstrip("-").isdigit() else observed
        passed = observed == spec["expected"] or (observed is None and spec["expected"] == 0)
        status = "PASS" if passed else {"error": "FAIL", "warn": "WARN", "info": "INFO"}[spec["severity"]]
        if spec["severity"] == "info":
            status = "INFO"
        record.update(observed=observed, status=status, detail=(row.get("detail") or "")[:500])
    except bronze.SqlError as exc:
        record.update(observed="", status="FAIL", detail=f"query error: {str(exc)[:500]}")
    return record


def null_profile(warehouse, columns_by_table):
    rows = []
    for name, columns in columns_by_table.items():
        counts = ", ".join(f"COUNT({bronze.quote(c)}) AS {bronze.quote('c' + str(i))}" for i, c in enumerate(columns))
        result = warehouse.execute(f"SELECT COUNT(*) AS n, {counts} FROM {S}.{name}")[0]
        total = int(result["n"])
        for i, column in enumerate(columns):
            nulls = total - int(result[f"c{i}"])
            rows.append({"table": name, "column": column, "rows": total, "nulls": nulls,
                         "null_rate": round(nulls / total, 6) if total else ""})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="ante")
    parser.add_argument("--warehouse-name", default="Serverless Starter Warehouse")
    parser.add_argument("--checks-only", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    cli = bronze.find_cli()
    warehouse = bronze.Warehouse(cli, args.profile, bronze.resolve_warehouse(cli, args.profile, args.warehouse_name, None))
    with open(BRONZE_AUDIT, newline="", encoding="utf-8") as f:
        audit_rows = list(csv.DictReader(f))
    if any(r["verification_status"] != "VERIFIED" for r in audit_rows):
        sys.exit("bronze audit has unverified tables; fix bronze first")
    bronze_counts = {r["table_name"].split(".")[-1]: int(r["databricks_row_count"]) for r in audit_rows}
    missing = [t["name"] for t in TABLES if t["name"] not in bronze_counts]
    if missing:
        sys.exit(f"no bronze source for {missing}")

    if not args.checks_only:
        warehouse.execute(f"CREATE SCHEMA IF NOT EXISTS {S}")
        #calendar and floor first: other tables join to the silver calendar
        first = [t for t in TABLES if t["name"] == "calendar_day"]
        rest = [t for t in TABLES if t["name"] != "calendar_day"]
        failures = []

        def build(spec):
            try:
                warehouse.execute(create_sql(spec))
                print(f"  built {S}.{spec['name']}", flush=True)
            except bronze.SqlError as exc:
                failures.append((spec["name"], str(exc)))
                print(f"  FAILED {S}.{spec['name']}: {exc}", flush=True)

        for spec in first:
            build(spec)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            list(pool.map(build, rest))
        if failures:
            sys.exit(f"{len(failures)} silver tables failed to build: {[name for name, _ in failures]}")

    add_generic_checks(bronze_counts)
    add_business_checks()
    print(f"running {len(CHECKS)} quality checks", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda c: run_check(warehouse, c), CHECKS))

    columns = warehouse.execute(f"SELECT table_name, column_name FROM workspace.information_schema.columns "
                                f"WHERE table_schema = 'ante_silver' ORDER BY table_name, ordinal_position")
    columns_by_table = {}
    for row in columns:
        columns_by_table.setdefault(row["table_name"], []).append(row["column_name"])
    profile = null_profile(warehouse, columns_by_table)
    counts = {t["name"]: next((r["observed"] for r in results if r["table"] == t["name"]
                               and r["check"] == "row count equals bronze"), "") for t in TABLES}

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    QUALITY_AUDIT.parent.mkdir(exist_ok=True)
    with open(QUALITY_AUDIT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["table", "check", "category", "severity", "expected", "observed", "status",
                                               "detail", "checked_at"])
        writer.writeheader()
        for record in results:
            writer.writerow({**record, "checked_at": checked_at})
    with open(NULL_PROFILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["table", "column", "rows", "nulls", "null_rate"])
        writer.writeheader()
        writer.writerows(profile)

    status_counts = {}
    for record in results:
        status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1
    print("\nsilver tables:")
    for spec in TABLES:
        print(f"  {spec['name']:<34} {counts[spec['name']]:>10,} rows  [{spec['origin']}"
              f"{', player-level' if spec['player_level'] else ''}]")
    print(f"\nchecks: {status_counts}")
    for record in results:
        if record["status"] in ("FAIL", "WARN", "INFO"):
            print(f"  {record['status']}: {record['table']}: {record['check']}: observed {record['observed']} "
                  f"(expected {record['expected']}) {record['detail']}")
    print(f"audit written to {QUALITY_AUDIT.relative_to(ROOT).as_posix()} and {NULL_PROFILE.relative_to(ROOT).as_posix()}")
    return 1 if status_counts.get("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
