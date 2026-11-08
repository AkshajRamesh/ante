#executive overview. the july scenario is a decision. floor results stay observed, and lower on the page.
from __future__ import annotations

import pandas as pd
import streamlit as st

from ante import charts, metrics
from ante.ui import ROOT, brand, frame, gold, money, percent

property_daily = frame("gold_property_daily")
machine_daily = frame("gold_machine_daily")
plan = frame("gold_investment_plan")
log = frame("gold_build_log")
bottom = frame("gold_bottom_ten")
scorecard = frame("gold_machine_scorecard")

first_day = property_daily["gaming_date"].min().date()
last_day = property_daily["gaming_date"].max().date()

try:
    summary = metrics.plan_summary(plan)
    mismatches = metrics.decision_mismatches(summary)
    counts = metrics.build_log_counts(log)
    original = metrics.list_named(bottom, "original")
    demand = metrics.list_named(bottom, "demand")
except ValueError as exc:
    st.error(str(exc), title="Gold plan could not be summarized")
    st.stop()

ranks = original.merge(
    scorecard.loc[:, ["machine_id", "rank_demand_index", "rank_theo_per_hour", "rank_actual_win_index"]],
    on="machine_id",
    how="left",
)
still_demand = len(set(original["machine_id"]) & set(demand["machine_id"]))

brand()
st.header("Don't replace the bottom ten.", anchor=False)
st.markdown(
    f"The $100,000 question is whether to pull the ten machines with the lowest house win. "
    f"**{still_demand} of those 10** are still in the bottom ten once demand is taken into account. "
    f"Spend **{money(summary['spend'])}** on an inspection and three moves. Leave **{money(summary['unallocated'])}**."
)

if mismatches:
    st.error(
        "Gold's plan totals do not round to the validated decision. Showing Gold's figures, not the expected ones. "
        + " ".join(mismatches),
        title="Plan check failed",
    )

with st.grid(3, gap="medium", min_column_width=180):
    st.metric(
        "To spend",
        money(summary["spend"]),
        border=True,
        height="stretch",
        help="Plan cost, taken once. Not a sum of the repeated budget column.",
    )
    st.metric(
        "Retained",
        money(summary["unallocated"]),
        border=True,
        height="stretch",
        help="Budget minus the plan cost. The money is retained, not assigned.",
    )
    st.metric(
        "Central net theo",
        money(summary["net_central"]),
        border=True,
        height="stretch",
        help=(
            f"July scenario, from {money(summary['net_low'])} to {money(summary['net_high'])}. "
            "Theoretical win after cost. Not money already earned."
        ),
    )
st.caption(
    f"Low case {money(summary['net_low'])}. High case {money(summary['net_high'])}. "
    "The range comes from the move assumptions."
)

st.plotly_chart(
    charts.demand_ranks(ranks),
    width="stretch",
    theme="streamlit",
    alt="Demand rank of the ten machines with the lowest house win. Rank 1 is the weakest demand. The dotted line is the bottom ten.",
)
st.caption(
    "Higher rank means stronger demand. The dotted line marks the bottom-ten threshold. "
    "None of the original ten remains in the demand-adjusted bottom ten."
)

st.header("Floor results", anchor=False)
st.caption("Observed. The date range does not change the July scenario above.")

chosen = st.date_input(
    "Gaming dates",
    value=(first_day, last_day),
    min_value=first_day,
    max_value=last_day,
    format="YYYY-MM-DD",
    help="Filters the observed cards and charts. The July plan is a scenario and does not change.",
    key="overview_dates",
)
if not (isinstance(chosen, tuple) and len(chosen) == 2 and chosen[0] and chosen[1]):
    st.info("Select a start date and an end date to see floor results.", title="Date range incomplete")
    st.stop()

start, end = chosen
view = metrics.between_dates(property_daily, start, end)
machines = metrics.between_dates(machine_daily, start, end)
if view.empty:
    st.warning("No gaming days fall in this range.", title="Nothing to show")
    st.stop()

kpis = metrics.property_kpis(view)
gap = metrics.machine_property_gap(machines, view)

with st.grid(4, gap="medium", min_column_width=180):
    st.metric(
        "Coin-in",
        money(kpis["coin_in"]),
        border=True,
        height="stretch",
        chart_data=kpis["spark_coin"],
        chart_type="area",
        help="Observed wagers on on-floor machines for the selected dates.",
    )
    st.metric(
        "House win",
        money(kpis["house_win"]),
        border=True,
        height="stretch",
        chart_data=kpis["spark_house"],
        chart_type="line",
        help="Observed coin-in minus payouts. This is not theoretical win.",
    )
    st.metric(
        "Theoretical win",
        money(kpis["theo"]),
        border=True,
        height="stretch",
        help="Expected house win under the published game math. Not added to house win, and not a forecast.",
    )
    st.metric(
        "Observed hold",
        percent(kpis["hold"]),
        border=True,
        height="stretch",
        help="House win divided by coin-in, both summed over the selected dates. Not an average of daily holds.",
    )

with st.grid(2, gap="medium", min_column_width=180):
    st.metric(
        "Availability",
        percent(kpis["availability"], 2),
        border=True,
        height="stretch",
        help="Available machine-hours divided by scheduled machine-hours.",
    )
    st.metric(
        "Downtime",
        f"{kpis['downtime_hours']:,.0f} h",
        border=True,
        height="stretch",
        help="Observed scheduled hours minus available hours.",
    )

if abs(gap) > 1:
    st.error(
        f"Machine-day coin-in and the property total differ by {money(gap)} for these dates. "
        "The charts are withheld so the two grains are not shown as if they agreed.",
        title="Floor totals do not reconcile",
    )
else:
    st.caption(f"{kpis['days']} gaming days. Machine-day coin-in matches the property total.")

series = view.sort_values("gaming_date")
st.plotly_chart(
    charts.money_lines(
        series,
        "Daily coin-in",
        [("coin_in_usd", "Coin-in", charts.IVORY)],
    ),
    width="stretch",
    theme="streamlit",
    alt="Daily observed coin-in in US dollars for the selected gaming dates",
)
with st.expander("House win against theoretical win"):
    st.plotly_chart(
        charts.money_lines(
            series,
            "Daily house win and theoretical win",
            [
                ("house_win_usd", "Observed house win", charts.IVORY),
                ("theo_usd", "Theoretical win", charts.GOLD),
            ],
        ),
        width="stretch",
        theme="streamlit",
        alt="Daily observed house win and theoretical win in US dollars. The two series are not added together.",
    )
    st.caption("Observed house win and theoretical win are drawn together and are not added.")

with st.expander("The four funded actions"):
    actions = plan.loc[:, ["action", "target", "detail", "to_zone", "target_cost_usd", "benefit_central_usd", "net_central_usd", "funding_basis"]].copy()
    actions = actions.rename(columns={
        "action": "Action",
        "target": "Target",
        "detail": "Detail",
        "to_zone": "To zone",
        "target_cost_usd": "Cost",
        "benefit_central_usd": "Central benefit",
        "net_central_usd": "Central net",
        "funding_basis": "Basis",
    })
    st.dataframe(
        actions,
        hide_index=True,
        width="stretch",
        alt="July plan actions with cost, central theoretical benefit, and central net",
        column_config={
            "Cost": st.column_config.NumberColumn(format="dollar"),
            "Central benefit": st.column_config.NumberColumn(format="dollar"),
            "Central net": st.column_config.NumberColumn(format="dollar"),
        },
    )
    st.caption("Net is theoretical win minus the action cost and the downtime cost. The inspection's benefit is zero.")

with st.expander("How to read these figures"):
    st.markdown(
        "Coin-in and house win are observed. Theoretical win is what the published game math expected. "
        "It is not added to house win. The July figures are a scenario for a later month, so the date filter does not change them. "
        "Hold and availability are ratios of sums over the selected dates. "
        f"Scope of the plan: {summary['scope']}"
    )

with st.expander("Provenance"):
    provenance = gold().provenance()
    st.markdown(
        f"Build `{provenance['build_id']}` at {provenance['built_at']} UTC, from `{provenance['source']}`. "
        f"All {provenance['tables']} snapshot files matched the manifest hash, row count, and column types before any figure was drawn."
    )
    st.markdown(
        f"The Gold build log records **{counts['pass']}** passed, **{counts['fail']}** failed, "
        f"**{counts['warn']}** warning, and **{counts['info']}** informational checks."
    )
    audit_path = ROOT / "audit" / "gold-quality-audit.csv"
    if audit_path.is_file():
        audit = pd.read_csv(audit_path)
        tally = audit["status"].value_counts().to_dict()
        st.markdown(
            "The local quality audit, which also includes snapshot parity checks, has "
            + ", ".join(f"**{tally.get(status, 0)}** {status}" for status in ("PASS", "FAIL", "WARN", "INFO"))
            + f" ({len(audit)} checks)."
        )
    st.caption(provenance.get("note") or "")
