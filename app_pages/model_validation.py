#frozen property forecast. the holdout is not refit, and a confidence interval is not a prediction interval.
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from ante import charts, metrics
from ante.ui import ROOT, frame, gold, money, percent

daily = frame("gold_forecast_daily")
validation = frame("gold_model_validation")
log = frame("gold_build_log")

HOLDOUT_SECTION = "holdout test (may-jun, frozen at 2025-04-30)"
HOLDOUT_PERIOD = "test may-jun"
MODELS = {
    "Weekday mean": ("weekday mean", "forecast_weekday_mean_usd", "abs_error_weekday_mean_usd", charts.GOLD),
    "Ridge": ("ridge (selected)", "forecast_ridge_usd", "abs_error_ridge_usd", charts.EMERALD),
    "Constant mean": ("constant mean", "forecast_constant_mean_usd", "abs_error_constant_mean_usd", charts.SLATE),
}
WEEKDAY_ORDER = charts.WEEKDAY_ORDER

try:
    rows = {
        label: metrics.one_row(validation, section=HOLDOUT_SECTION, model=model, period=HOLDOUT_PERIOD, metric="mae")
        for label, (model, _column, _error, _color) in MODELS.items()
    }
    difference = metrics.one_row(
        validation,
        section="paired holdout comparison (week-block bootstrap)",
        model="ridge - weekday mean",
        period=HOLDOUT_PERIOD,
        metric="mae difference (usd; negative = first model more accurate)",
    )
    coverage = metrics.one_row(
        validation,
        section="interval calibration (holdout)",
        model="ridge (selected)",
        period=HOLDOUT_PERIOD,
        metric="share of days inside the nominal 90% interval",
    )
    disclosure = metrics.one_row(
        validation,
        section="disclosure",
        model="weekday mean",
        period=HOLDOUT_PERIOD,
        metric="lowest holdout mae (usd)",
    )
    counts = metrics.build_log_counts(log)
except ValueError as exc:
    st.error(str(exc), title="Forecast evidence is missing")
    st.stop()

holdout = daily.loc[daily["sample"].astype(str).str.startswith("holdout")].copy()
training = daily.loc[daily["sample"].astype(str).str.startswith("training")].copy()
origin = holdout["forecast_origin"].dropna().unique()
covered_days = int(holdout["actual_in_ridge_interval"].fillna(False).sum())

if len(holdout) != 61 or holdout["gaming_date"].min().date() != date(2025, 5, 1) or holdout["gaming_date"].max().date() != date(2025, 6, 30):
    st.error("The holdout is not the 61 days from May 1 through June 30, 2025.", title="Holdout window does not match")
if len(training) != 120 or training["gaming_date"].min().date() != date(2025, 1, 1) or training["gaming_date"].max().date() != date(2025, 4, 30):
    st.error("The training window is not January 1 through April 30, 2025.", title="Training window does not match")
if len(origin) != 1 or pd.Timestamp(origin[0]).date() != date(2025, 4, 30):
    st.error("The holdout forecast origin is not April 30, 2025.", title="Forecast origin does not match")
if float(rows["Weekday mean"]["value"]) >= float(rows["Ridge"]["value"]):
    st.error("The weekday mean is no longer the lowest holdout MAE.", title="Model ranking does not match")
if not (float(difference["ci_low"]) < 0 < float(difference["ci_high"])):
    st.error("The ridge-minus-weekday confidence interval no longer includes zero.", title="Comparison interval does not match")
if abs(float(disclosure["value"]) - float(rows["Weekday mean"]["value"])) > 1:
    st.error("The disclosure row does not match the weekday-mean holdout MAE.", title="Disclosure does not reconcile")
if covered_days != 31 or abs(float(coverage["value"]) - covered_days / 61) > 0.001:
    st.error(
        f"Ridge interval coverage is {covered_days} days, not the published {float(coverage['value']):.1%} of 61.",
        title="Interval coverage does not match",
    )
for label, (_model, _column, error_column, _color) in MODELS.items():
    recomputed = float(holdout[error_column].mean())
    published = float(rows[label]["value"])
    if abs(recomputed - published) > 1:
        st.error(
            f"{label} holdout MAE recomputed from the daily errors is {money(recomputed)}, not the published {money(published)}.",
            title="Holdout error does not reconcile",
        )

def signed_money(value) -> str:
    value = float(value)
    body = money(abs(value))
    return f"-{body}" if value < 0 else body


st.header("The weekday baseline beat ridge.")
st.markdown(
    f"Holdout error is **{money(rows['Weekday mean']['value'])}** for the weekday mean and "
    f"**{money(rows['Ridge']['value'])}** for ridge. "
    f"The difference is **{signed_money(difference['value'])}**, "
    f"and the 95% confidence interval **{signed_money(difference['ci_low'])}** to **{signed_money(difference['ci_high'])}** includes zero."
)
with st.grid(3, gap="medium", min_column_width=180):
    for label in ("Weekday mean", "Ridge", "Constant mean"):
        row = rows[label]
        st.metric(
            label,
            money(row["value"]),
            border=True,
            height="stretch",
            help=(
                f"Mean absolute error on the 61 held-out days, in dollars of coin-in. "
                f"95% confidence interval {money(row['ci_low'])} to {money(row['ci_high'])}. "
                "This interval is about the error, not a band around a day's forecast."
            ),
        )
mae_frame = pd.DataFrame({
    "label": list(rows),
    "estimate": [float(row["value"]) for row in rows.values()],
    "ci_low": [float(row["ci_low"]) for row in rows.values()],
    "ci_high": [float(row["ci_high"]) for row in rows.values()],
})
st.plotly_chart(
    charts.dollar_intervals(mae_frame, "label", "Holdout mean absolute error", "USD of coin-in"),
    width="stretch",
    theme="streamlit",
    alt="Holdout mean absolute error for the weekday mean, ridge, and constant mean, with 95 percent confidence intervals",
)
st.caption("Lower is better. The bars are 95% confidence intervals for the average error. They are not prediction intervals.")
detail = []
for label, (model, _column, _error, _color) in MODELS.items():
    mape = metrics.one_row(validation, section=HOLDOUT_SECTION, model=model, period=HOLDOUT_PERIOD, metric="mape")
    rmse = metrics.one_row(validation, section=HOLDOUT_SECTION, model=model, period=HOLDOUT_PERIOD, metric="rmse")
    bias = metrics.one_row(validation, section=HOLDOUT_SECTION, model=model, period=HOLDOUT_PERIOD, metric="mean_error")
    detail.append({
        "Model": label,
        "MAE": money(rows[label]["value"]),
        "MAPE": percent(mape["value"], 1),
        "RMSE": money(rmse["value"]),
        "Mean error": signed_money(bias["value"]),
    })
with st.expander("MAPE, RMSE, and mean error"):
    st.dataframe(pd.DataFrame(detail), hide_index=True, width="stretch")
    st.caption("Mean error is forecast minus actual. A positive value means the forecast ran high.")

st.caption(
    "One forecast of daily property coin-in, frozen on April 30 for May 1 through June 30. "
    "January through April was used to build and choose the model. Those days do not overlap the test."
)

with st.container(border=True):
    chosen = st.pills(
        "Forecasts",
        options=list(MODELS),
        selection_mode="multi",
        default=["Weekday mean", "Ridge"],
        key="forecast_models",
        help="Turns series on or off. It does not refit anything.",
    )
    window = st.segmented_control(
        "Days on the chart",
        options=["Holdout only", "Holdout and training fits"],
        default="Holdout only",
        key="forecast_window",
        help="Training lines are in-sample fits. They are not the April 30 forecast.",
    )

selected = list(chosen or [])
show_training = window == "Holdout and training fits"
plotted = daily if show_training else holdout
series = [
    (column, label, color)
    for label, (_model, column, _error, color) in MODELS.items()
    if label in selected
]
if plotted.empty or not series:
    st.info("Select at least one forecast to draw the chart.", title="No forecast selected")
else:
    st.plotly_chart(
        charts.forecast_compare(
            plotted.sort_values("gaming_date"),
            series,
            "Actual coin-in and the frozen forecasts",
            show_band="Ridge" in selected,
        ),
        width="stretch",
        theme="streamlit",
        alt="Daily actual machine coin-in against the selected forecasts. The ridge band is a nominal 90 percent prediction interval on the holdout only.",
    )
    if show_training:
        st.caption(
            "The shaded period is the holdout. Dotted lines are January–April fits to those same days, so they are not a test. "
            "Solid lines are the forecast frozen on April 30. "
            "The green band, shown with ridge, is a nominal 90% prediction interval. It is not a confidence interval, and the baselines do not have one."
        )
    else:
        st.caption(
            "Solid lines are the forecast frozen on April 30. May and June outcomes were not used to change it. "
            "The green band, shown with ridge, is a nominal 90% prediction interval. It is not a confidence interval, and the baselines do not have one."
        )

st.subheader("The nominal 90% interval is not reliable")
with st.grid(2, gap="medium", min_column_width=220):
    st.metric(
        "Days inside the 90% interval",
        f"{covered_days} of {int(coverage['days'])}",
        border=True,
        height="stretch",
        help="Count of holdout days whose actual coin-in fell between the published ridge low and high. Nominal coverage was 90%.",
    )
    st.metric(
        "Ridge minus weekday MAE",
        signed_money(difference["value"]),
        border=True,
        height="stretch",
        help=(
            f"95% confidence interval {signed_money(difference['ci_low'])} to {signed_money(difference['ci_high'])}. "
            "The interval includes zero, so the holdout does not separate the two models."
        ),
    )
st.warning(
    f"A 90% prediction interval covered {percent(coverage['value'])} of the holdout, {covered_days} of 61 days. "
    "It is poorly calibrated and understates uncertainty. Do not read that band as a 90% chance.",
    title="Prediction interval",
    icon=":material/warning:",
)

selection = validation.loc[
    (validation["section"] == "model selection (mean mae over validation folds)")
    & (validation["metric"] == "mean fold mae")
    & (validation["model"].isin(["ridge #54", "weekday mean", "constant mean"]))
].copy()
selection["label"] = selection["model"].map({
    "ridge #54": "Ridge",
    "weekday mean": "Weekday mean",
    "constant mean": "Constant mean",
})
compare = pd.DataFrame({"label": ["Weekday mean", "Ridge"]})
compare["Validation folds"] = compare["label"].map(selection.set_index("label")["value"])
compare["Holdout"] = compare["label"].map({label: float(row["value"]) for label, row in rows.items()})
st.plotly_chart(
    charts.grouped_bars(
        compare,
        "label",
        [
            ("Validation folds", "February–April selection", charts.SLATE),
            ("Holdout", "May–June holdout", charts.GOLD),
        ],
        "Selection error and holdout error",
        "Mean absolute error, USD",
    ),
    width="stretch",
    theme="streamlit",
    alt="Mean absolute error on the February to April selection folds and on the untouched May to June holdout",
)
st.markdown(
    "Ridge won the February–April expanding-window folds, so it was the model taken forward. "
    "That comparison used only dates through April 30. On the untouched May–June days, the weekday mean was lower. "
    "A model can track demand and still add nothing beyond the weekly pattern. "
    f"The constant mean is off this scale: about {money(float(selection.loc[selection['label']=='Constant mean', 'value'].iloc[0]))} "
    f"on the folds and {money(rows['Constant mean']['value'])} on the holdout."
)

st.subheader("Where the weekday pattern points")
by_weekday = pd.DataFrame({"weekday": WEEKDAY_ORDER})
by_weekday["January–April mean"] = by_weekday["weekday"].map(
    training.groupby("local_weekday")["actual_coin_in_usd"].mean()
)
by_weekday["May–June actual"] = by_weekday["weekday"].map(
    holdout.groupby("local_weekday")["actual_coin_in_usd"].mean()
)
st.plotly_chart(
    charts.grouped_bars(
        by_weekday,
        "weekday",
        [
            ("January–April mean", "January–April mean, the baseline", charts.GOLD),
            ("May–June actual", "May–June actual", charts.IVORY),
        ],
        "Coin-in by weekday",
        "USD",
    ),
    width="stretch",
    theme="streamlit",
    alt="Average daily coin-in by weekday in the training window and in the holdout",
)
st.caption(
    "Tuesday is the low day in the training means, and it stayed the low day in May and June. Wednesday is next. "
    "Saturday is the high day. The weekday baseline is that pattern. It does not need a regression to find it."
)

decision = validation.loc[
    (validation["section"] == "decision value (holdout)")
    & (validation["model"].isin([
        "weekday-mean forecast: lowest day each week",
        "ridge forecast: lowest day each week",
        "fixed monday each week",
        "oracle: actual lowest day (not achievable)",
    ]))
].copy()
decision["label"] = decision["model"].map({
    "weekday-mean forecast: lowest day each week": "Weekday mean",
    "ridge forecast: lowest day each week": "Ridge",
    "fixed monday each week": "Fixed Monday",
    "oracle: actual lowest day (not achievable)": "Oracle, not achievable",
})
decision["label"] = pd.Categorical(
    decision["label"],
    ["Weekday mean", "Ridge", "Oracle, not achievable", "Fixed Monday"],
    ordered=True,
)
decision = decision.sort_values("label")
st.plotly_chart(
    charts.grouped_bars(
        decision,
        "label",
        [("value", "Actual coin-in on the chosen day", charts.GOLD)],
        "Actual coin-in on the chosen maintenance day",
        "USD",
    ),
    width="stretch",
    theme="streamlit",
    alt="Mean actual coin-in on the one day per week each rule would have chosen",
)
weekday_choice = float(decision.loc[decision["label"] == "Weekday mean", "value"].iloc[0])
ridge_choice = float(decision.loc[decision["label"] == "Ridge", "value"].iloc[0])
ten_day = validation.loc[
    (validation["section"] == "decision value (holdout)")
    & (validation["model"].isin([
        "weekday-mean forecast: 10 lowest days (ties by date)",
        "ridge forecast: 10 lowest days",
    ]))
]
ten_values = {row.model: float(row.value) for row in ten_day.itertuples()}
st.markdown(
    f"Choosing the lowest forecast day in each full week exposed **{money(weekday_choice)}** of actual coin-in under the weekday rule "
    f"and **{money(ridge_choice)}** under ridge. "
    "Those are the same published result, and they match the oracle that peeks at the outcome. "
    f"Picking any 10 days in May and June gives the same tie: **{money(ten_values['weekday-mean forecast: 10 lowest days (ties by date)'])}** "
    f"for the weekday rule and **{money(ten_values['ridge forecast: 10 lowest days'])}** for ridge. "
    "Ridge did not improve the maintenance choice. The figure is demand on the disrupted day, not money saved."
)

with st.expander("How the forecast was frozen"):
    st.markdown(
        "The target is daily property coin-in. Features had to be known on April 30: weekday, announced events, and closure days already past. "
        "Three expanding-window folds forecast February, March, and April from origins on January 31, February 28, and March 31. "
        "The selected ridge uses event type, a trend, a log target, and alpha 0.1. It does not use a holiday flag or the closure flag. "
        "Its nominal 90% band multiplies the forecast by residual quantiles from those folds. "
        "The holdout confidence intervals come from 4,000 draws of the nine test week-blocks. "
        "That interval is about the difference in average error. The green band on the chart is a different object: a prediction interval for one day's coin-in."
    )

st.divider()
st.subheader("Where these numbers come from")
bronze_path = ROOT / "audit" / "bronze-load-audit.csv"
silver_path = ROOT / "audit" / "silver-quality-audit.csv"
gold_path = ROOT / "audit" / "gold-quality-audit.csv"
bronze = pd.read_csv(bronze_path) if bronze_path.is_file() else pd.DataFrame()
silver = pd.read_csv(silver_path) if silver_path.is_file() else pd.DataFrame()
gold_audit = pd.read_csv(gold_path) if gold_path.is_file() else pd.DataFrame()
snapshot_tables = gold().provenance()["tables"]
analytical_tables = snapshot_tables - 1

def tally(frame: pd.DataFrame, column: str) -> dict:
    if frame.empty or column not in frame.columns:
        return {}
    return frame[column].value_counts().to_dict()

bronze_status = tally(bronze, "verification_status")
silver_status = tally(silver, "status")
gold_status = tally(gold_audit, "status")
silver_tables = int(silver.loc[silver["table"] != "*", "table"].nunique()) if not silver.empty else 0

with st.container(horizontal=True):
    steps = [
        ("Bronze", f"{len(bronze)} source tables", f"{bronze_status.get('VERIFIED', 0)} verified loads"),
        ("Silver", f"{silver_tables} cleaned tables", f"{silver_status.get('PASS', 0)} pass, {silver_status.get('INFO', 0)} info, {silver_status.get('FAIL', 0)} fail"),
        ("Gold", f"{analytical_tables} analytical tables", f"Build log {counts['pass']} pass, {counts['fail']} fail, {counts['warn']} warn"),
        ("This app", f"{snapshot_tables} snapshot files", "Verified parquet. No live query."),
    ]
    for title, line, detail in steps:
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.markdown(line)
            st.caption(detail)
st.markdown(
    "Python produced the forecast, the closure comparison, and the July plan. "
    "Gold recomputed the additive sums, reconciled them to those outputs, and failed the build on a mismatch. "
    f"The local Gold audit, including snapshot parity, has **{gold_status.get('PASS', 0)}** pass, "
    f"**{gold_status.get('FAIL', 0)}** fail, **{gold_status.get('WARN', 0)}** warning, and **{gold_status.get('INFO', 0)}** info "
    f"({len(gold_audit)} checks). "
    "The public dashboard reads that verified snapshot. It does not query Databricks."
)
