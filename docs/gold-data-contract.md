# Gold data contract: `workspace.ante_gold`

Gold is the publishable layer for the **ante** dashboard. `scripts/build-gold.py` builds it from Silver (`workspace.ante_silver`) and from the verified Python analysis outputs in `outputs/`, then validates it with the checks in `audit/gold-quality-audit.csv`. Bronze, Silver, the analysis scripts and their outputs are read only.

## Ground rules

- **Python results are authoritative for complex estimates.** Difference-in-differences, bootstraps, regressions, the forecast and the July 2025 plan are loaded verbatim from `outputs/*.csv` (uploaded to the volume `workspace.ante_gold.analysis_outputs` and SHA-256 verified). Gold never re-estimates them with different SQL logic.
- **Simple sums are recomputed from Silver and reconciled** against the matching CSV (machine totals, zone and property days, downtime, monthly and segment player totals, comps). A difference fails the build; the original calculation is never edited to make it pass.
- **Value classes are kept apart.** Every money column is one of: observed (`coin_in_usd`, `house_win_usd`, `payout_usd`, `comp_cost_usd`); theoretical (`theo_usd`, expected win under the published game math); estimate (`est_gross_lost_*`, evidence estimates with confidence intervals); forecast (`forecast_*`); or scenario (`benefit_*`, `net_*`, `value_*`). Observed and theoretical are never added together, and forecasts and scenarios are never shown as realized results.
- **Ratios are ratios of sums.** Filterable tables hold additive daily sums. A dashboard ratio (hold, availability, theo per available hour) is `SUM(numerator) / SUM(denominator)` over the filtered rows, never an average of daily ratios.
- **No labels of fraud or malfunction.** Flags say "requires investigation" and describe the statistical pattern. Nothing targets heavy-loss players.
- **Rebuilds are idempotent.** Every table is `CREATE OR REPLACE`d on each run, and the outputs volume is overwritten.

## Privacy

- No Gold table contains player-level rows. Each table has the properties `ante.contains_player_level=false` and `ante.publishable=true`. The build fails if any Gold table or snapshot file has a column from the forbidden list: `player_id`, `observed_visit_id`, `session_segment_id`, `loyalty_event_id`, `transaction_id`, `comp_id`, `redeemed_pos_id`, `source_segment_id`, `assignment_id`, `incident_id`.
- Guest data appears only in `gold_player_segments`. It is aggregated by month (or the six-month window) and one pre-play attribute: opening tier, home region, age band or enrollment cohort.
- **Suppression rule.** A cell is suppressed when `players_seen < 20`, or when 1–19 of its players had carded play. If exactly one cell in a (period, dimension) is suppressed, the next-smallest cell is also suppressed, so the hidden cell can't be recovered by subtracting from the total. Suppressed cells keep their labels, and every measure is set to null. In the current build no cell meets the threshold: the smallest is 129 players, Platinum in a single month. The rule is checked on every build.
- Segments use attributes known before play. Groupings that depend on outcomes, such as wager bands or end-of-window tier, are not published (`grouping_depends_on_outcomes = false`).
- `gold_downtime_tickets` contains operational tickets for machines, not guest records.

## Scope labels

Every table and most rows have a `scope` column that tells the dashboard how the numbers may be filtered.

| Label | Meaning |
| --- | --- |
| filterable | Additive daily sums. Any date, zone, machine or game filter is valid; compute ratios as ratios of sums. |
| fixed: whole floor, 2025-01-01 to 2025-06-30 | Computed once by Python over the full window (ranks, z-scores, adjusted indices, evidence). Show it as fixed. Don't recompute it under filters. |
| fixed: frozen forecast | Whole-property daily coin-in forecast made on 2025-04-30 for May–June. Not filterable by zone, machine or game. |
| fixed: July 2025 plan | Expected-win (theo) scenarios for the July 1–30 plan horizon. Not realized win. |
| segments | Filterable by month and one guest attribute. Small cells are suppressed. |

## Tables

Row counts are from build `26107b2d4a24`. Keys are unique and non-null (checked).

| Table | Questions | Grain | Key | Rows | Scope |
| --- | --- | --- | --- | ---: | --- |
| `gold_machine_daily` | Q1, Q2, Q3 | machine × gaming date, on-floor days only | `machine_id, gaming_date` | 55,092 | filterable |
| `gold_machine_scorecard` | Q1, Q3 | machine, six-month window | `machine_id` | 310 | fixed window |
| `gold_bottom_ten` | Q1 | list × rank | `list_name, list_rank` | 40 | fixed window |
| `gold_zone_daily` | Q2, Q3 | zone × gaming date | `zone_id, gaming_date` | 1,267 | filterable |
| `gold_property_daily` | Q2, Q3, Q5 | gaming date | `gaming_date` | 181 | filterable |
| `gold_downtime_tickets` | Q3 | maintenance or move ticket | `operation_id` | 1,226 | filterable |
| `gold_player_segments` | Q4, Q5 | period × dimension × segment | `period_month, dimension, segment` | 105 | segments |
| `gold_forecast_daily` | forecast | gaming date | `gaming_date` | 181 | frozen forecast |
| `gold_model_validation` | forecast | section × model × period × metric | `section, model, period, metric` | 57 | frozen forecast |
| `gold_investment_plan` | Q6 | plan action × target | `action_type_id, target` | 4 | July plan |
| `gold_scenarios` | Q2, Q3, Q6 | scenario | `scenario_id` | 35 | mixed, labelled per row |
| `gold_evidence` | Q1–Q6 | statistical result | `evidence_id` | 409 | fixed window |
| `gold_build_log` | all | build × table | `build_id, gold_table` | 12 | build metadata |

### Table notes

- **`gold_machine_daily`** holds Silver `prepared_daily_view` active rows (56,110 minus 1,018 off-floor rows) joined to `machine_day` and `game_math`. It adds `closure_period`, `carded_theo_usd`, scheduled and downtime hours, and additive test parts:
  - `hit_spins`, `expected_hit_spins`, `hit_variance`
  - `luck_residual_usd`, `luck_variance_usd2`

  These let the dashboard recompute a valid luck z or hit z for any filtered set:
  - luck z = `SUM(luck_residual_usd) / sqrt(SUM(luck_variance_usd2))`
  - hit z = `(SUM(hit_spins) - SUM(expected_hit_spins)) / sqrt(SUM(hit_variance))`

  Off-floor days are excluded, not counted as zero performance.
- **`gold_machine_scorecard`** is `machine-performance.csv` joined with `downtime-machines.csv`. It adds flags:
  - `flag_payout_pattern`: |hit z| > 4
  - `flag_luck_unlikely`: Bonferroni p < 0.05
  - `flag_excess_faults`: fault excess z ≥ 3
  - `flag_low_demand`, `flag_partial_window`
  - `requires_investigation`, with plain-language `review_notes`

  Four machines currently require investigation. M-1158 is flagged for its payout pattern. M-1240, M-1241 and M-1206 are flagged for faults well above their manufacturer's rate.
- **`gold_bottom_ten`** holds four lists of ten:
  - original (total house win)
  - exposure-adjusted (theo per available hour)
  - demand-adjusted (demand index within zone)
  - actual-win-adjusted (actual win index)

  `in_original_bottom_10` shows overlap with the original list. None of the original list members appear in the demand-adjusted list.
- **`gold_zone_daily`** has 7 zones × 181 days. Machine meters come from `gold_machine_daily`, and traffic and sensor coverage from `zone_traffic_hourly`. Sensor coverage hours are stored so traffic can be weighted by coverage.
- **`gold_property_daily`** combines:
  - `property_day` meters
  - downtime by cause and estimated lost play from `downtime-daily.csv`
  - loyalty points and redemptions from `loyalty_ledger`
  - F&B ticket counts from `fnb_transactions`

  Comp cost is counted once: `non_comp_cogs_usd = pos_cogs_usd - comp_cost_usd`. `comp_face_value_usd` is menu value, not cost, and is never added to cost or cash.
- **`gold_downtime_tickets`** holds Silver `operations` plus zone, config and manufacturer. It also has `est_gross_lost_*` per ticket, which is the zone-based estimate from `downtime-lost-play.csv`. That figure is gross: it does not net out play that moved to other machines.
- **`gold_forecast_daily`** has actuals and three forecasts per day:
  - `sample` separates the training (in-sample) rows from the 61 frozen holdout days.
  - `actual_in_ridge_interval` marks whether the actual fell inside the ridge interval.
- **`gold_model_validation`** sections:
  - holdout test
  - in-sample fit (labelled "not validation")
  - chronological folds and model selection
  - paired holdout comparison
  - interval calibration
  - decision value
  - a `disclosure` row naming the model with the lowest holdout MAE
- **`gold_investment_plan`** holds the validated plan rows with costs, low, central and high `benefit_*`, and `net_* = benefit - cost - downtime cost`. It also carries the assumptions, controls, observation period, confounders and decision rule. Plan-level `budget_usd`, `plan_total_cost_usd` and `unallocated_usd` repeat on each row, so don't sum them across rows.
- **`gold_scenarios`** combines:
  - Q6 alternatives and sensitivity
  - Q3 maintenance action economics and lost play
  - Q2 closure impact

  `kind` labels each row as a decision scenario, counterfactual estimate, hypothetical not in the plan, sensitivity-only row, or infeasible. Only rows marked "in plan net" add up to the plan.
- **`gold_evidence`** has one row per statistical result. Each row carries an estimate, a CI, an `evidence_class` and a `causal_interpretation`. Associations are always labelled "not causal", and the build checks this.

## Join contract

- The daily tables share `gaming_date`, and zone tables share `zone_id`. Machine tables join on `machine_id`. Machine-level game attributes (`config_id`, `theme`, `manufacturer`) are already stored on `gold_machine_daily` as of each day, so no join is needed to filter by game.
- Don't join a daily table to the scorecard and then sum daily values grouped by a scorecard column: machines that moved would be counted under their whole-window `primary_zone`. Filter by `gold_machine_daily.zone_id` instead.
- Never join `gold_player_segments` to another table, and never sum across dimensions. Each (period, dimension) is a complete partition of guests. Each 'ALL' row counts distinct players across six months, so it is not the sum of the monthly rows.
- Plan-level columns in `gold_investment_plan` and window-level columns in `gold_scenarios` are repeated per row. Take them once.

## Reconciliations with existing results

The checks that tie Gold to Silver and to the Python outputs include:

- **Q1:**
  - per-machine sums
  - luck z and hit z recomputed from `gold_machine_daily`
  - M-1158 hit z = 124.19 and luck z = −4.57
  - bottom-ten lists re-ranked in SQL
  - the scorecard-to-downtime join
- **Q2/Q3:**
  - zones sum to property, and machine-days sum to `property_day`
  - zone and property days match the demand, loyalty and downtime CSVs
  - downtime causes add up to downtime hours
  - ticket hours by cause equal the daily cause allocation
  - lost theo matches `downtime-lost-play.csv`
- **Q4/Q5:**
  - monthly totals match `players-monthly.csv` and `loyalty-monthly.csv`
  - six-month segments match `players-groups.csv` and `loyalty-groups.csv`
  - comp total equals the Silver ledger economic cost
  - comp tickets carry no cash
- **Forecast:**
  - actuals equal `property_day`
  - 61 holdout days
  - MAE recomputed from the daily rows
  - interval coverage recomputed
- **Q6:**
  - cost $6,600, unallocated $93,400, at most three action types
  - rows sum to the recommended alternative
  - the inspection has zero benefit, with basis "information"

**Known label difference (warning, not a failure).** In `investment-move-plan.csv`, the columns named `net_*` actually hold benefit before move cost and downtime. That doesn't match how `net` is used in `investment-candidates.csv`. Gold publishes them as `benefit_*` and computes `net_*` itself. Gold's benefit sums match `investment-recommended-plan.csv` `benefit_*`. Plan net is $2,059 / $13,272 / $22,724 (low / central / high). The CSV was not edited.

**Visits definition.** `players-groups.csv` counts visits only for players with carded play. Gold publishes both `observed_visits` (all guests seen) and `observed_visits_players_with_play` (the Python definition). The reconciliation uses the second.

### M-1158: hit z 124 vs luck z −4.57

Both values are recomputed in Databricks from the Silver meters and match the Q1 output. They are consistent, not contradictory:

- **Hit z (124.19)** tests how often spins pay. The hit rate is 0.3857 against a published 0.3154. Each spin carries little noise, so a 7-point gap over many spins gives a very large z.
- **Luck z (−4.57, Bonferroni p = 0.0015 across 310 machines)** tests the money result: house win against theo. Observed hold is 3.04% against a configured 10%. This test is much noisier because payout sizes vary, so the same underlying deviation produces a smaller z.

**Supported claim:** M-1158's payout pattern does not match its published configuration on two related measures. It is a lead to inspect, not evidence of a malfunction or wrongdoing. The Q6 inspection buys information. It has zero assumed benefit and is not treated as guaranteed recovery. Any correction would be a separate, separately costed decision.

### Forecast disclosure

On the 61-day holdout (May–June, frozen at 2025-04-30), MAE by model:

| Model | Holdout MAE | 95% CI |
| --- | ---: | --- |
| weekday mean | $68,699 | $55,766–$81,823 |
| ridge (selected) | $73,652 | $58,202–$88,991 |
| constant mean | $218,258 | — |

Ridge minus weekday mean = $4,954 (95% CI −$1,293 to $11,150). The interval includes zero, so the two models are not clearly different, and the simpler baseline is not beaten. The ridge 90% interval covered only 31 of 61 holdout days (51%). It is too narrow and understates uncertainty. Show it with that caveat, or don't show it.

## Dashboard page mapping

| Page | Gold tables | Filters | Fixed-scope elements (label as fixed) |
| --- | --- | --- | --- |
| Overview | `gold_property_daily`, `gold_build_log` | date | build id and check counts |
| Q1 Machine performance | `gold_machine_daily`, `gold_machine_scorecard`, `gold_bottom_ten`, `gold_evidence` (topic Q1) | date, zone, machine, game (`config_id`/`theme`), on daily rows | bottom-ten lists, ranks, adjusted indices, flags, M-1158 reconciliation |
| Q2 Demand and floor layout | `gold_zone_daily`, `gold_property_daily`, `gold_machine_daily`, `gold_evidence` (Q2), `gold_scenarios` (Q2) | date, zone, `closure_period` | closure DiD, placebos, relocation experiments, demand drivers (associations) |
| Q3 Downtime | `gold_downtime_tickets`, `gold_zone_daily`, `gold_property_daily`, `gold_machine_scorecard`, `gold_scenarios` (Q3), `gold_evidence` (Q3) | date, zone, machine, cause, manufacturer, shift | fault-rate models, maintenance cost-vs-benefit scenarios |
| Q4 Guests | `gold_player_segments`, `gold_evidence` (Q4) | month, one dimension at a time | Gini, concentration, luck calibration |
| Q5 Comps | `gold_player_segments`, `gold_property_daily`, `gold_evidence` (Q5) | month or date, `comp_policy_period`, one dimension | policy before/after, local comparison |
| Forecast | `gold_forecast_daily`, `gold_model_validation` | holdout or in-sample toggle only | everything; frozen forecast |
| Q6 July plan | `gold_investment_plan`, `gold_scenarios` (Q6), `gold_evidence` (Q6) | none (plan view) | everything; theo scenarios, not realized win |

Use one filter dimension at a time on `gold_player_segments`. Don't cross segment filters, because those cells are not published.

## Deployment design

The dashboard must be public, with no Databricks login for viewers.

- **Host:** Streamlit Community Cloud, serving only Gold.
- **Live mode:** `databricks-sql-connector` against the SQL warehouse, using a dedicated read-only identity: a service principal or token with `USE CATALOG`/`USE SCHEMA`/`SELECT` on `workspace.ante_gold` only, and no Silver or Bronze grants. The host and token live in Streamlit secrets (`.streamlit/secrets.toml` is git-ignored), never in the repository. Queries are parameterized and read-only, and results are cached.
- **Snapshot fallback:** if secrets are missing or the warehouse is unavailable, the app reads `snapshot/*.parquet`. Every build writes these together with `snapshot/manifest.json`, which records the build id, per-file SHA-256, row counts and column types. The app checks the manifest hashes before loading and shows the snapshot's build id and build time.
- **Parity:** the snapshot is exported only when every check passes. Snapshot checks compare row counts and every numeric column sum against the live table. In live mode, the app can compare `gold_build_log.build_id` with the manifest and show a notice if they differ.
- The snapshot is aggregated Gold only. It contains no player-level rows and is safe to commit.

## Lineage, freshness and rebuild

- `audit/gold-lineage.csv` lists, for each Gold table, every Silver table or output file it uses, with source SHA-256 for files, row count and build id. Each table also records its sources in the `ante.sources` property.
- `audit/gold-quality-audit.csv` holds one row per check: table, check, category, expected, observed, status, detail, build id and timestamp. `gold_build_log` stores the same counts in Databricks.
- **Freshness checks:**
  - every Gold table was rebuilt in this run, after Silver's last change
  - every output CSV is at least as new as the script that produces it (warning only)
  - stale or unexpected files on the volume are reported

**Rebuild:**

```powershell
.\.venv\Scripts\python.exe scripts\build-gold.py               # full build, checks, snapshot
.\.venv\Scripts\python.exe scripts\build-gold.py --checks-only # re-run checks on the existing tables
```

The script authenticates with the Databricks CLI profile `ante` (OAuth; no secrets in code). It stops before building if the Silver audit has any FAIL or a required output file is missing. It exits with status 1 on any failed check, and in that case the snapshot is not written.
