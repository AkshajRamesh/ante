#q2. filterable demand is observed. the closure estimate and the four moves are fixed.
from __future__ import annotations

import pandas as pd
import streamlit as st

from ante import charts, metrics
from ante.ui import frame, money, signed_percent

zones = frame("gold_zone_daily")
property_daily = frame("gold_property_daily")
evidence = frame("gold_evidence")
scenarios = frame("gold_scenarios")

closure_days = zones.loc[zones["closure_period"] == "closure", "gaming_date"]
closure_start = closure_days.min()
closure_end = closure_days.max()
first_day = zones["gaming_date"].min().date()
last_day = zones["gaming_date"].max().date()
zone_names = sorted(zones["zone_name"].dropna().unique().tolist())

try:
    decline = metrics.one_row(
        evidence, question="Q2", topic="back forty walkway closure",
        metric="coin_in_per_avail_hour", subgroup="all machines in zone",
        comparison="all other zones | closure vs pre",
    )
    recovery = metrics.one_row(
        evidence, question="Q2", topic="back forty walkway closure",
        metric="coin_in_per_avail_hour", subgroup="all machines in zone",
        comparison="all other zones | post vs closure (recovery)",
    )
    afterward = metrics.one_row(
        evidence, question="Q2", topic="back forty walkway closure",
        metric="coin_in_per_avail_hour", subgroup="all machines in zone",
        comparison="all other zones | post vs pre",
    )
    pretrend = metrics.one_row(
        evidence, question="Q2", topic="back forty walkway closure",
        metric="coin_in_per_avail_hour", subgroup="all machines in zone",
        comparison="all other zones | pre-trend: last 30 pre days vs earlier pre days",
    )
    traffic = metrics.one_row(
        evidence, question="Q2", topic="back forty walkway closure",
        metric="entries_per_covered_hour", subgroup="all machines in zone",
        comparison="all other zones | closure vs pre",
    )
    walkway = metrics.one_row(
        evidence, question="Q2", topic="property demand drivers", metric="walkway closure period",
    )
    lost_coin = metrics.one_row(scenarios, question="Q2", scenario="lost coin-in")
    lost_theo = metrics.one_row(scenarios, question="Q2", scenario="lost theo")
except ValueError as exc:
    st.error(str(exc), title="Closure evidence is missing")
    st.stop()

if abs(float(decline["estimate"]) + 0.308) > 0.001:
    st.warning(
        f"The published Back Forty comparison is {signed_percent(decline['estimate'])}, "
        "which does not round to the familiar −30.8%. The figure on this page is the Gold value.",
        title="Check the closure estimate",
    )

st.header(f"Back Forty moved {signed_percent(decline['estimate'])} while the walkway was closed.")
st.markdown(
    "Coin-in per available hour, relative to every other zone. "
    "Foot traffic fell on the same comparison. The property-wide association for that window does not clear zero. "
    f"{decline['causal_interpretation']}"
)
with st.grid(3, gap="medium", min_column_width=180):
    st.metric(
        "Closure vs before",
        signed_percent(decline["estimate"]),
        border=True,
        height="stretch",
        help=f"Interval {signed_percent(decline['ci_low'])} to {signed_percent(decline['ci_high'])}. Observational. Not a proven cause.",
    )
    st.metric(
        "After vs during",
        signed_percent(recovery["estimate"]),
        border=True,
        height="stretch",
        help=f"Recovery relative to the other zones. Interval {signed_percent(recovery['ci_low'])} to {signed_percent(recovery['ci_high'])}.",
    )
    st.metric(
        "Entries per covered hour",
        signed_percent(traffic["estimate"]),
        border=True,
        height="stretch",
        help="Same closure-versus-before comparison, using sensor entries instead of coin-in.",
    )

levels = metrics.zone_rates(zones, ["zone_name", "closure_period"])
st.plotly_chart(
    charts.period_bars(levels, "Coin-in per hour, before, during, and after", "coin_per_available_hour", "USD per available hour"),
    width="stretch",
    theme="streamlit",
    alt="Observed coin-in per available hour by zone before, during, and after the closure",
)
st.caption(
    f"Observed levels. After versus before, Back Forty was still {signed_percent(afterward['estimate'])} relative to the other zones. "
    f"The property regression term for the closure is {signed_percent(walkway['estimate'])}, and its interval includes zero."
)

with st.container(border=True):
    date_choice = st.date_input(
        "Gaming dates",
        value=(first_day, last_day),
        min_value=first_day,
        max_value=last_day,
        format="YYYY-MM-DD",
        key="demand_dates",
        help="Filters the observed trend and the zone comparison. It does not recompute the closure estimate.",
    )
    zone_choice = st.pills(
        "Zones on the trend",
        options=zone_names,
        selection_mode="multi",
        default=["Back Forty"],
        key="demand_zones",
        help="Leave unselected to draw every zone. The comparison bars always include every zone.",
    )

if not (isinstance(date_choice, tuple) and len(date_choice) == 2 and date_choice[0] and date_choice[1]):
    st.info("Select a start date and an end date.", title="Date range incomplete")
    st.stop()

selected = metrics.between_dates(zones, date_choice[0], date_choice[1])
if zone_choice:
    selected_lines = selected.loc[selected["zone_name"].isin(zone_choice)]
else:
    selected_lines = selected
if selected.empty or selected_lines.empty:
    st.warning("No zone-days match these filters.", title="Empty selection")
    st.stop()

selected_lines = selected_lines.copy()
selected_lines["coin_per_available_hour"] = selected_lines["coin_in_usd"] / selected_lines["available_hours"].where(selected_lines["available_hours"] > 0)
selected_lines["entries_per_covered_hour"] = selected_lines["entries_observed"] / selected_lines["covered_sensor_hours"].where(selected_lines["covered_sensor_hours"] > 0)

window_rates = metrics.zone_rates(selected, ["zone_name"])
floor_coin = float(window_rates["coin_in_usd"].sum())
floor_hours = float(window_rates["available_hours"].sum())
with st.grid(3, gap="medium", min_column_width=180):
    st.metric("Coin-in", money(floor_coin), border=True, height="stretch", help="Observed wagers in the selected dates, summed across zones.")
    st.metric(
        "Coin-in per hour",
        money(metrics.ratio(floor_coin, floor_hours), 0),
        border=True,
        height="stretch",
        help="Coin-in divided by available machine-hours. A ratio of sums, not an average of daily rates.",
    )
    st.metric(
        "Zones",
        f"{window_rates['zone_name'].nunique()}",
        border=True,
        height="stretch",
        help="Zones with on-floor hours in the selected dates.",
    )

trend = charts.zone_rate_lines(
    selected_lines,
    "Coin-in per available hour",
    "coin_per_available_hour",
    "USD per available hour",
)
charts.shade_closure(trend, closure_start, closure_end + pd.Timedelta(days=1))
st.plotly_chart(trend, width="stretch", theme="streamlit", alt="Daily coin-in per available hour for the selected zones, with the closure shaded")
st.caption(f"The band is the closure, {closure_start:%b %d}–{closure_end:%b %d}, {int(closure_days.nunique())} gaming days. April 7 is the first day after.")

with st.expander("Property coin-in and the zone levels"):
    property_view = metrics.between_dates(property_daily, date_choice[0], date_choice[1]).sort_values("gaming_date")
    property_chart = charts.money_lines(
        property_view,
        "Property coin-in",
        [("coin_in_usd", "Observed coin-in", charts.IVORY)],
    )
    charts.shade_closure(property_chart, closure_start, closure_end + pd.Timedelta(days=1))
    st.plotly_chart(property_chart, width="stretch", theme="streamlit", alt="Daily observed property coin-in, with the walkway closure shaded")
    st.plotly_chart(
        charts.value_bars(window_rates, "zone_name", "coin_per_available_hour", "Zone comparison for the selected dates", "USD per available hour"),
        width="stretch",
        theme="streamlit",
        alt="Observed coin-in per available hour by zone for the selected dates",
    )
    st.caption("These bars are observed levels for the dates above. They are not the closure estimate.")

st.subheader("What the closure does not prove")
with st.grid(3, gap="medium", min_column_width=180):
    st.metric(
        "Pre-trend",
        signed_percent(pretrend["estimate"], 2),
        border=True,
        height="stretch",
        help=f"Last 30 days before the closure versus the earlier pre period. Interval {signed_percent(pretrend['ci_low'], 2)} to {signed_percent(pretrend['ci_high'], 2)}.",
    )
    st.metric(
        "Counterfactual coin-in",
        money(lost_coin["value_central_usd"]),
        border=True,
        height="stretch",
        help=f"Not observed revenue. Range {money(lost_coin['value_low_usd'])} to {money(lost_coin['value_high_usd'])}. {lost_coin['assumptions']}",
    )
    st.metric(
        "Counterfactual theo",
        money(lost_theo["value_central_usd"]),
        border=True,
        height="stretch",
        help=f"Theoretical win, not cash kept. Range {money(lost_theo['value_low_usd'])} to {money(lost_theo['value_high_usd'])}.",
    )

closure_window = f"window from {pd.Timestamp(closure_start):%Y-%m-%d}"
placebo_time = evidence.loc[
    (evidence["question"] == "Q2")
    & (evidence["subgroup"] == "placebo time: Back Forty")
    & (evidence["metric"] == "coin_in_per_avail_hour")
    & (evidence["comparison"] != closure_window)
]
placebo_zones = evidence.loc[
    (evidence["question"] == "Q2")
    & evidence["subgroup"].astype(str).str.startswith("placebo zone:")
    & (evidence["subgroup"] != "placebo zone: Back Forty")
    & (evidence["metric"] == "coin_in_per_avail_hour")
]
if not placebo_time.empty and not placebo_zones.empty:
    st.caption(
        "Placebo windows on Back Forty, leaving out the closure window itself, stay between "
        f"{signed_percent(placebo_time['estimate'].min())} and {signed_percent(placebo_time['estimate'].max())}. "
        "The same comparison on the other zones stays between "
        f"{signed_percent(placebo_zones['estimate'].min())} and {signed_percent(placebo_zones['estimate'].max())}. "
        "Those checks do not reproduce the closure drop. They support the comparison; they do not prove it."
    )

st.subheader("Four earlier moves")
move_measure = st.segmented_control(
    "Move result",
    options=["Coin-in per hour", "Occupancy"],
    default="Coin-in per hour",
    key="move_measure",
)
move_metric = "coin-in per hour vs peers" if move_measure != "Occupancy" else "occupancy vs peers"
moves = evidence.loc[
    (evidence["question"] == "Q2")
    & (evidence["topic"] == "machine relocations")
    & (evidence["metric"] == move_metric)
].copy()
if moves.empty or move_measure is None:
    st.info("Choose a move result to see the four historical relocations.", title="No move chart")
else:
    moves["label"] = moves["subgroup"].astype(str) + " · " + moves["comparison"].astype(str)
    st.plotly_chart(
        charts.effect_bars(moves, "Change versus same-configuration peers", "label"),
        width="stretch",
        theme="streamlit",
        alt="Relative change after four historical machine moves, with confidence intervals",
    )
    best = moves.loc[moves["estimate"].idxmax()]
    worst = moves.loc[moves["estimate"].idxmin()]
    st.caption(
        f"{best['subgroup']} {signed_percent(best['estimate'])} after {best['comparison']}. "
        f"{worst['subgroup']} {signed_percent(worst['estimate'])} after {worst['comparison']}. "
        f"{moves['causal_interpretation'].iloc[0]} A new relocation is not guaranteed to match either result."
    )

st.subheader("What else moved with demand")
events = evidence.loc[
    (evidence["question"] == "Q2")
    & (evidence["topic"] == "property demand drivers")
    & evidence["metric"].astype(str).str.startswith("event:")
].copy()
events["label"] = events["metric"].astype(str).str.removeprefix("event: ")
st.plotly_chart(
    charts.effect_bars(events, "Event associations with daily coin-in", "label"),
    width="stretch",
    theme="streamlit",
    alt="Regression associations between calendar events and property coin-in, with intervals",
)
st.caption(f"{events['causal_interpretation'].iloc[0].capitalize()}. Weekends are a larger association than any event; they are not shown here so the events stay readable.")

with st.expander("How the closure estimate is defined"):
    st.markdown(
        "The −30.8% figure is a difference-in-differences on coin-in per available hour: Back Forty's change from the pre period "
        "to the closure, minus the same change in all other zones. It is not Back Forty's own raw percent drop, and it is not "
        "the property regression term for the closure window. Pre-trend and placebo rows are checks, not extra effects to add. "
        "The counterfactual dollars assume Back Forty would have kept its previous gap to the other zones. They are not money the cage counted."
    )
