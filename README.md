# ante

The ten lowest-earning slots are the wrong ten to replace.

**[Live Dashboard](https://ante-tamu-datathon.streamlit.app)** · **[Source Code](https://github.com/AkshajRamesh/ante)**

The Gilded Ace, a fictional casino, has $100,000 to invest and is considering replacing its ten lowest-earning slot machines. We tested whether raw earnings identify underperformance once exposure, demand, and the published game math are taken into account.

**Don't replace the bottom ten.**

## The decision

| | |
| --- | ---: |
| Budget | $100,000 |
| Recommended spend | $6,600 |
| Unallocated | $93,400 |
| Low / central / high net theoretical win | +$2,059 / +$13,272 / +$22,724 |

Inspect M-1158 for $1,200. Relocate M-1154 and M-1184 to Center Pit, and M-1157 to Hotel Corridor, at $1,800 each.

Those figures are prospective July scenarios for net theoretical win, not observed or guaranteed earnings. The unspent $93,400 is the recommendation: the larger options are over budget, negative after cost, or rest on an association that is not causal. Assumptions and the review rule are on [Investment Decision](https://ante-tamu-datathon.streamlit.app/investment).

## Key findings

January–June 2025. Six-month ranks and the July plan stay fixed when a date filter changes.

| Area | Key finding | Decision implication |
| --- | --- | --- |
| [Machine performance](https://ante-tamu-datathon.streamlit.app/machines) | 0 of the original bottom ten remain in the demand-index bottom ten. Eight remain among the ten weakest on theoretical win per available hour. M-1158’s hold is 3.04% against a 10% theoretical hold. | Inspect M-1158. Do not replace the raw bottom ten. |
| [Demand](https://ante-tamu-datathon.streamlit.app/demand) | Back Forty coin-in per available hour fell 30.8% relative to other zones during the walkway closure. Traffic fell 31.4% on the same comparison. | Association, not a proven cause. Test three relocations. |
| [Downtime](https://ante-tamu-datathon.streamlit.app/downtime) | Availability was 99.62%. Downtime was 5,021 hours. | Maintenance alternatives did not justify their cost. |
| [Players](https://ante-tamu-datathon.streamlit.app/players) | The top 5% of carded wagerers account for 52.1% of $163.8 million in carded wagers. | Aggregates only. No guest-level action. |
| [Loyalty](https://ante-tamu-datathon.streamlit.app/players) | Six-month comp cost was $190,669.30 and is already inside F&B cost of goods. | Leave comps unchanged. That cost is already counted. |
| [Investment](https://ante-tamu-datathon.streamlit.app/investment) | One inspection and three relocations, instead of replacing the bottom ten. | Spend $6,600. Leave $93,400 unallocated. |

## Model validation

The forecast predicts daily property-level machine coin-in. Training and selection use January–April 2025, the origin is frozen on April 30, and the holdout is May 1–June 30: 61 days that were not used to choose the model.

| Model | Holdout MAE |
| --- | ---: |
| Weekday mean | $68,699 |
| Ridge regression | $73,652 |
| Constant mean | $218,258 |

Ridge won the February–April selection folds, so it was carried forward. On the holdout, the weekday mean was lower. The ridge-minus-weekday MAE gap is $4,954, and its 95% interval, from −$1,293 to $11,150, includes zero. The nominal 90% prediction interval covered 31 of 61 days. Both models picked the same lower-demand maintenance days, so ridge added no decision value. Charts and methods are on [Model Validation](https://ante-tamu-datathon.streamlit.app/validation).

## Databricks

- **Data pipeline:** Raw Parquet → Bronze → Silver → Gold → Verified Snapshot → Streamlit
- **AI assistant:** Verified Snapshot → Ask ante → Gemini API

| Layer | Purpose | Validation |
| --- | --- | --- |
| Bronze | 31 ingested Delta tables | 31 verified loads |
| Silver | 31 validated tables | 222 pass, 0 fail, 7 info |
| Gold | 12 analytical tables | 117 pass, 0 fail, 1 warning, 4 info |

The public app reads a manifest-verified Gold snapshot (build `26107b2d4a24`), not a live Databricks query. The one Gold warning is a mislabeled source column; Gold recomputes net, and the plan totals match.

[Bronze](scripts/load-bronze.py) · [Silver](scripts/build-silver.py) · [Gold](scripts/build-gold.py) · [Silver contract](docs/silver-data-contract.md) · [Gold contract](docs/gold-data-contract.md) · [Bronze audit](audit/bronze-load-audit.csv) · [Silver audit](audit/silver-quality-audit.csv) · [Gold audit](audit/gold-quality-audit.csv) · [Lineage](audit/gold-lineage.csv) · [Manifest](snapshot/manifest.json)

## Ask ante, powered by Gemini

Ask ante is the floating assistant on every page. It calls Google's Gemini API, answers from a retrieved packet of verified Gold facts, and checks that cited numbers and scenario language stay tied to that packet. A July projection is not presented as guaranteed revenue. Gemini does not compute the original statistics or query Databricks.

[ante/analyst.py](ante/analyst.py) · [ante/evidence.py](ante/evidence.py) · [ante/ask_chat.py](ante/ask_chat.py)

## Reproducibility

The Gilded Ace is fictional. Guest-level records and raw datasets are not published. The app checks [snapshot/manifest.json](snapshot/manifest.json) — hash, row count, and column types — before it draws. The original statistical scripts are not in this repository, so the estimates cannot be rebuilt from raw rows here. Closure results are observational. July figures are scenarios, not guaranteed outcomes. ChatGPT and Cursor coding agents (Claude and Grok) assisted development. Gemini powers Ask ante.

## Run locally

Python 3.12. The seven pages run from the snapshot and need no Databricks credentials. On macOS or Linux, use `python3` in place of `python`.

```powershell
git clone https://github.com/AkshajRamesh/ante.git
cd ante
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

Open http://localhost:8501. Ask ante needs an untracked `.streamlit/secrets.toml`. Without it, the rest of the dashboard still runs.

```toml
GEMINI_API_KEY = "your-key"
```
