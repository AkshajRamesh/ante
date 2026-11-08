# Silver data contract: `workspace.ante_silver`

Silver is the analysis-ready layer for **ante**. It is built from the verified Bronze tables (`workspace.ante_bronze`) by `scripts/build-silver.py` and validated by the checks in `audit/silver-quality-audit.csv`. Bronze is read only. Gold tables and the dashboard are built later from Silver.

## Ground rules

- **Same rows, same columns, same types.** Each Silver table has exactly its Bronze row count, every Bronze column with the identical data type, and the original identifiers. Nothing is dropped, deduplicated away, filled in or re-signed. Negative house win, net player losses and payouts above 100% are valid values.
- **Only additive transformations.** Silver adds a few derived helper columns (listed per table). It replaces Bronze `source_file`/`ingested_at` with `bronze_source_file` and drops `prepared_daily_view._rescued_data` (null on all 56,110 rows; an artifact of `read_files`).
- **Timestamps** stay as they are in the source: `*_utc` columns are `TIMESTAMP_NTZ` holding UTC wall-clock time, `*_local` columns are `TIMESTAMP_NTZ` in America/Los_Angeles. Gaming dates are `DATE`.
- **Gaming-date mapping** for event tables uses the half-open interval `[gaming_day_start_utc, gaming_day_end_utc)` from `calendar_day`, the same rule as the analysis scripts. Calendar days are contiguous and non-overlapping (checked), so each event maps to at most one day. Events outside the window get a null gaming date and are kept.
- **Money** keeps its source type: integer `*_cents` (BIGINT) or exact `*_usd` (DECIMAL). No floating-point conversion.
- **Rebuilds are idempotent**: every table is `CREATE OR REPLACE`d from Bronze on each run.

## Data origin and privacy

Every table carries table properties `ante.layer`, `ante.data_origin`, `ante.contains_player_level` and `ante.grain`.

- `synthetic`: the fictional Gilded Ace casino. Never present as facts about a real casino.
- `real_public`: unmodified public data, only in tables prefixed `real_`. Every row keeps `source_url` and `source_sha256` (checked). Never union or sum real rows with synthetic rows. Tourism and visitor-profile data describe all Las Vegas visitors, were published after 2025, and must not feed the chronological forecast.
- **Player-level tables** (`players`, `sessions`, `trips`, `loyalty_ledger`, `fnb_transactions`, `player_period`) stay inside Databricks. Anything exported or shown publicly must be aggregated to groups (for example tier, region, segment, month) with small groups suppressed. Do not publish `player_id`, `observed_visit_id` or row-level guest records. Do not build anything that targets heavy-loss players.

## Tables

Row counts are from the latest build. Keys are unique and non-null (checked) unless noted.

### Reference and calendar

| Table | Grain | Key | Rows | Added columns | Used for |
| --- | --- | --- | ---: | --- | --- |
| `calendar_day` | gaming date | `gaming_date` | 181 | none | all; gaming-day mapping, DST (2025-03-08 = 82,800 s) |
| `floor_locations` | zone, bank, slot, outlet or entrance | `location_id` | 378 | `zone_name` (from the zone node) | Q1, Q2 location hierarchy |
| `floor_walkways` | walkway | `walkway_id` | 10 | none | Q2 renovation closure |
| `machines` | machine | `machine_id` | 310 | none | Q1, Q3 |
| `game_math` | game configuration | `config_id` | 10 | none | theo, hit rate, Q1 payout checks |
| `placements` | machine x location/config interval | `placement_id` | 314 | `effective_from_gaming_date`, `last_gaming_date` (inclusive) | Q1, Q2 moves; interval joins |
| `cost_rules` | supplied cost rule | `cost_rule_id` | 9 | `rate_value` (exact decimal of text `rate`) | contribution, Q6 |
| `action_catalog` | budget action | `action_type_id` | 13 | none | Q6 prices, limits, lead times |
| `events` | calendar event | `event_id` | 8 | none | Q2, forecast; use `announced_date` for as-of logic |

### Machine and floor facts

| Table | Grain | Key | Rows | Added columns | Used for |
| --- | --- | --- | ---: | --- | --- |
| `machine_day` | machine x gaming date, all play | `machine_id, gaming_date` | 56,110 | none | Q1, Q3 base meters in cents |
| `prepared_daily_view` | machine x gaming date, joined, dollars | `machine_id, gaming_date` | 56,110 | none (`_rescued_data` dropped) | Q1-Q3, Q6 |
| `property_day` | gaming date, whole property | `gaming_date` | 181 | none | forecast, Q2, Q5 |
| `machine_hour` | machine x hour while installed | `machine_id, hour_start_utc` | 1,321,908 | none | Q3 hourly downtime |
| `operations` | ticket | `operation_id` | 1,226 | `opened_gaming_date`, `unavailable_seconds`, `unresolved` | Q2 moves, Q3 downtime |
| `zone_traffic_hourly` | sensor x hour | `sensor_id, hour_start_utc` | 30,401 | `gaming_date` | Q2 traffic |
| `security_incidents` | reported incident | `incident_id` | 3,865 | `occurred_gaming_date` | context only; reports are leads, not findings |
| `shift_roster` | staff assignment | `assignment_id` | 10,317 | none | Q3 technician capacity |

`machine_day` and `prepared_daily_view` contain 1,018 off-floor rows (machine not yet installed or already retired; `active` false, `location_id` null). Exclude them from performance metrics; they are not zero performance. Use `available_hours`/`available_seconds` as exposure.

### Guests (player-level, internal only)

| Table | Grain | Key | Rows | Added columns | Used for |
| --- | --- | --- | ---: | --- | --- |
| `players` | loyalty account | `player_id` | 113,890 | none | Q4, Q5 |
| `sessions` | carded session segment | `session_segment_id` | 776,297 | none | Q4, Q5; carded play only |
| `trips` | observed property visit | `observed_visit_id` | 351,729 | `first_gaming_date` | Q4 visit frequency |
| `loyalty_ledger` | points award or redemption | `loyalty_event_id` | 782,773 | `event_gaming_date`, `in_analysis_window` | Q5 |
| `fnb_transactions` | POS ticket, carded or not | `transaction_id` | 227,833 | `gaming_date`, `is_comp` | Q5, property contribution |
| `player_period` | tracked player x month | `player_id, period_month` | 148,893 | none | Q4, Q5 |
| `visitor_profile_monthly` | fictional survey metric x month | `month, survey_metric` | 42 | none | context (synthetic survey) |

### Real public context

| Table | Grain | Key (checked as unique) | Rows |
| --- | --- | --- | ---: |
| `real_weather_daily` | NOAA station x calendar date (not gaming date) | `station_id, source_date` | 181 |
| `real_city_visitation_monthly` | month x geography | `month, geography` | 6 |
| `real_visitor_profile_summary` | survey metric | `metric_id, segment_group, segment` | 74 |
| `real_visitor_profile_segments` | metric x segment (segments overlap) | `metric_id, segment_group, segment` | 1,406 |
| `real_benchmark_public` | Missouri casino x slot category x month | `property_id, category, month` | 700 |
| `real_benchmark_property_monthly` | Missouri casino x month | `property_id, month` | 78 |
| `real_benchmark_state_monthly` | month | `month` | 6 |

Do not add `real_benchmark_property_monthly` to `real_benchmark_public`; they are alternative totals of the same results.

## Join contract

All joins below are many-to-one on a checked unique key unless stated. Aggregate fact tables to the target grain before joining them to each other; never join two fact tables at row level and then sum.

| From | To | On | Cardinality |
| --- | --- | --- | --- |
| `machine_day`, `prepared_daily_view`, `sessions`, `placements` | `machines` / `game_math` | `machine_id` / `config_id` | N:1 |
| any table with `gaming_date` | `calendar_day` | `gaming_date` | N:1 |
| event tables (`*_utc` instants) | `calendar_day` | `ts >= gaming_day_start_utc AND ts < gaming_day_end_utc` | N:1 (already materialized as the added gaming-date columns) |
| `machine_day` | `placements` | `machine_id` and `effective_from_utc <= gaming_day_start_utc < coalesce(effective_to_utc, far future)` | N:1; placements never overlap per machine or per slot (checked) |
| `prepared_daily_view` | `machine_day` | `machine_id, gaming_date` | 1:1 |
| `sessions` | `trips` | `observed_visit_id` | N:1; same `player_id` (checked) |
| `sessions`, `trips`, `loyalty_ledger`, `player_period` | `players` | `player_id` | N:1 |
| `loyalty_ledger` (redeem) | `fnb_transactions` | `redeemed_pos_id = transaction_id` | 1:1 (checked both ways) |
| `fnb_transactions` (comp) | `loyalty_ledger` | `comp_id = loyalty_event_id` | 1:1 |
| `loyalty_ledger` (award) | `sessions` | `source_segment_id = session_segment_id` | N:1 |
| `zone_traffic_hourly`, `security_incidents`, `shift_roster` | `floor_locations` (zones) | `zone_id` | N:1 |

## Money rules the checks enforce

- **Actual vs theoretical.** House win = coin-in - payout (exact cents, may be negative). Theo = coin-in x (1 - configured RTP), checked for every machine-day and session; it is expected win, not realized win. Luck residual: machine tables house win - theo; player tables net + theo.
- **No revenue double counting.** `property_day` equals the sum of machine-days; `machine_hour` sums to `machine_day`; carded session totals equal `machine_day.carded_*`; `player_period` equals sessions by player and gaming month. Use one of these levels per metric, never a sum across levels.
- **Comps counted once.** Comp cost lives inside F&B cost of goods. Every comp ticket has exactly one redemption whose `economic_cost_cents` equals the ticket's `cogs_cents`; comp tickets carry no cash receipt; total comp cogs equal total redemption economic cost (19,066,930 cents). Subtract either `fnb_transactions.cogs_cents` (which includes comps) or the redemption cost, never both. Comp face value is menu price, not revenue.
- **Ratios from sums.** Hold, hit rate, occupancy and reinvestment are computed as ratios of summed numerators and denominators at the reporting grain.
- **Missing is not zero.** `zone_traffic_hourly.entries_count` is null exactly when the counter is offline (checked); off-floor machine-days are null, not zero.

## Leakage protection

Silver adds no forward-looking or cross-day features. The supplied `lag_*` columns in `prepared_daily_view` are checked to use only prior gaming days. For as-of logic use `events.announced_date`, `loyalty_ledger.available_at_utc` and `operations.recorded_at_utc`. Real tourism and visitor-profile tables were published after 2025 and cannot be forecast inputs.

## Quality checks

`scripts/build-silver.py` runs about 230 checks after every build and writes:

- `audit/silver-quality-audit.csv`: table, check, category, severity, expected, observed, status (`PASS`, `FAIL`, `WARN`, `INFO`) and detail. The script exits non-zero on any `FAIL`.
- `audit/silver-null-profile.csv`: null count and rate for every Silver column.

Categories: row count vs Bronze, key uniqueness and nulls, exact duplicates, schema/type preservation, date ranges, referential integrity, many-to-many guards, money invariants, theo vs actual, comp double-count guards, missing-is-not-zero, leakage, data origin. `INFO` rows record anomalies that are preserved for review rather than corrected:

- M-1158 hit rate is far above its published hit probability (z = 124). This is a lead to inspect, not a finding of fraud or malfunction.
- One loyalty award (284 points) at 2025-07-01 13:00 UTC falls after the last gaming day; it is kept and flagged `in_analysis_window = false`.
- 153 offline counter hours have null entries.
- 7,117 machine-days with negative house win and 109,404 player-months with a net loss are valid outcomes.

## Rebuild

```sh
python scripts/load-bronze.py --verify-only   # bronze must be fully verified first
python scripts/build-silver.py                # rebuild and validate silver
python scripts/build-silver.py --checks-only  # validate without rebuilding
```

Both scripts authenticate through the Databricks CLI profile (`--profile ante` by default) and never store credentials.
