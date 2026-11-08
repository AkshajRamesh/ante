# ante

ante is a floor-strategy dashboard for **The Gilded Ace**, a fictional casino. It answers the six Dashboard Royale questions with verified Gold figures, and it keeps observed results, statistical estimates, forecasts, and the July investment scenario apart.

The public app does not query Databricks. It reads the sanitized snapshot in `snapshot/` and checks `snapshot/manifest.json` (SHA-256, row count, column names, and column types) before drawing a figure. A failed check stops the page instead of inventing a number.

## The six questions

1. **Machine performance.** Raw house win is the wrong replacement list. None of the original bottom ten remains in the demand-adjusted bottom ten. Eight of the ten are still among the weakest on theoretical win per available hour.
2. **Demand and location.** Back Forty coin-in per available hour fell about 30.8% while the walkway was closed, relative to other zones. That is a conditional association, not proof that the closure caused the drop. Four earlier relocations are a small sample.
3. **Downtime and operations.** The floor stayed about 99.62% available. The large maintenance options were priced and left out of the July plan.
4. **Players.** Carded play is concentrated. A few tracked players place most of the wagers. Figures are group totals, not guest records.
5. **Loyalty.** Comps and the May 1 points-price change are reported as associations. The change is not treated as a proven cause.
6. **Investment.** Of a $100,000 July budget, the plan spends **$6,600** and leaves **$93,400** unspent. The funded actions are one inspection of M-1158 ($1,200) and three machine moves. Central net theoretical benefit is **$13,272**, with a low case of **$2,059** and a high case of **$22,724**. Those are July scenarios, not money already earned. M-1158 is an inspection lead because its hit rate and hold diverge from the published game math. It is not a fraud or malfunction finding.

## Bronze, Silver, Gold

| Layer | Where it lives | What this repository contains |
| --- | --- | --- |
| Bronze | Databricks, loaded from the source files | `scripts/load-bronze.py` |
| Silver | Databricks, same rows as Bronze plus a few derived columns | `scripts/build-silver.py` and `docs/silver-data-contract.md` |
| Gold | Aggregated tables the dashboard is allowed to publish | `scripts/build-gold.py`, `docs/gold-data-contract.md`, and `snapshot/` |

Reproducing that pipeline requires the original datasets and a Databricks workspace. The scripts are the engineering record. They are not run by the dashboard.

`audit/` holds the aggregate check reports from the build: row counts, key checks, null rates, and lineage. Those files name columns such as `player_id` when a check targets that column. They do not contain guest rows.

Player-level Silver tables stay in Databricks. Gold fails the build if a publishable table contains `player_id` or the other forbidden identifiers. Guest figures in the snapshot are segments. A cell is suppressed below 20 players. In this build the smallest published cell is 129 players.

## Ask ante

Ask ante is a floating assistant on every page, powered by Gemini. It answers from a small packet of validated Gold facts, checks that cited figures appear in that packet, and labels the source, for example `Investment Decision · Q6`. It does not run SQL or Python, and it does not receive guest-level records.

Each browser session allows 30 questions. Opening the panel does not call the API.

### Configure the key

Create `.streamlit/secrets.toml` locally. That file is gitignored.

```toml
GEMINI_API_KEY = "your-key"
```

On Streamlit Community Cloud, paste the same line into the app's Advanced settings secrets. Do not commit the key, print it, or put it in source.

Without a key, the seven analytical pages still run. Ask ante reports that Gemini is not configured.

## Run locally

Python 3.11 or newer.

```powershell
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

Open http://localhost:8501.

Leave `ANTE_GOLD_SOURCE` unset, or set it to `snapshot`. Setting it to `databricks` stops the app. Live warehouse reads are not connected, and the app will not pretend the snapshot is a live query.

## Limits

- Ratios are ratios of sums, not averages of daily ratios.
- A date filter changes observed floor totals. It does not recompute the six-month ranks, the forecast, or the July plan.
- The weekday-average forecast had a lower May–June holdout error than ridge. Ridge did not win the holdout.
- The forecast's nominal 90% band is a prediction interval for one day. It is not the confidence interval on the holdout error.
- The snapshot is a fixed build (`26107b2d4a24`). Refreshing the dashboard does not refresh Databricks.
