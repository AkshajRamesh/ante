#q6. the july plan is a fixed scenario. filters on other pages do not change it.
from __future__ import annotations

import pandas as pd
import streamlit as st

from ante import charts, metrics
from ante.ui import frame, money, percent

plan = frame("gold_investment_plan")
scenarios = frame("gold_scenarios")
evidence = frame("gold_evidence")

try:
    summary = metrics.plan_summary(plan)
    mismatches = metrics.decision_mismatches(summary)
    recommended = metrics.one_row(
        scenarios,
        question="Q6",
        scenario_group="budget alternatives (net theo over the plan horizon)",
        item="recommended",
    )
except ValueError as exc:
    st.error(str(exc), title="The July plan is missing")
    st.stop()

if mismatches:
    st.error(
        "Gold's plan totals do not round to the validated decision. Showing Gold's figures. "
        + " ".join(mismatches),
        title="Plan check failed",
    )
if abs(float(plan["target_cost_usd"].sum()) - summary["spend"]) > 1:
    st.error(
        f"Action costs sum to {money(plan['target_cost_usd'].sum())}, not the plan total of {money(summary['spend'])}.",
        title="Spend does not reconcile",
    )
if abs(float(recommended["value_central_usd"]) - summary["net_central"]) > 1:
    st.error(
        "The recommended scenario does not match the sum of the action nets.",
        title="Scenario net does not reconcile",
    )

moves = plan.loc[plan["action_type_id"] == "A-MOVE-MACHINE"].copy()
inspection = plan.loc[plan["target"] == "M-1158"]
if inspection.empty or moves.empty:
    st.error("The inspection or the relocations are missing from the plan.", title="Plan rows are missing")
    st.stop()
move_rule = moves.iloc[0]

st.header(f"Spend {money(summary['spend'])}. Leave {money(summary['unallocated'])} unspent.")
st.markdown(
    "Inspect M-1158 and move three Back Forty machines. "
    f"Central net theoretical win is **{money(summary['net_central'])}**, "
    f"from **{money(summary['net_low'])}** to **{money(summary['net_high'])}**. "
    "Those are July scenarios, not money already earned."
)
st.caption(str(plan["scope"].iloc[0]))

with st.grid(4, gap="medium", min_column_width=180):
    st.metric("Spend", money(summary["spend"]), border=True, height="stretch", help="Sum of the four action costs. Taken once, not summed from the repeated budget column.")
    st.metric("Left unspent", money(summary["unallocated"]), border=True, height="stretch", help="Budget minus the plan cost. Retained, not assigned to another action.")
    st.metric("Central net theo", money(summary["net_central"]), border=True, height="stretch", help="Sum of per-target net theoretical win in the central case, after action cost and downtime cost.")
    st.metric(
        "Net theo range",
        f"{money(summary['net_low'])} to {money(summary['net_high'])}",
        border=True,
        height="stretch",
        help="Low and high cases from the published move assumptions. Not a confidence interval on past earnings.",
    )

labels = []
values = []
for _, row in plan.iterrows():
    if row["target"] == "M-1158":
        labels.append("Inspect M-1158")
    else:
        labels.append(f"Move {row['target']} to {row['to_zone']}")
    values.append(float(row["target_cost_usd"]))
labels.append("Retained")
values.append(summary["unallocated"])
st.plotly_chart(
    charts.allocation_bars(labels, values, "How the $100,000 is used"),
    width="stretch",
    theme="streamlit",
    alt="July budget split across four actions and the unspent balance",
)
st.caption("Cash allocated on July 1. Retained is the unspent balance, not a forecast of profit.")

actions = plan.loc[:, [
    "action", "target", "from_location", "to_zone", "to_location",
    "target_cost_usd", "target_downtime_cost_usd",
    "benefit_central_usd", "net_low_usd", "net_central_usd", "net_high_usd",
    "scheduled_date", "effect_days", "downtime_minutes",
]].copy()
actions = actions.rename(columns={
    "action": "Action",
    "target": "Machine",
    "from_location": "From",
    "to_zone": "To zone",
    "to_location": "To spot",
    "target_cost_usd": "Cost",
    "target_downtime_cost_usd": "Downtime cost",
    "benefit_central_usd": "Central benefit",
    "net_low_usd": "Low net",
    "net_central_usd": "Central net",
    "net_high_usd": "High net",
    "scheduled_date": "Scheduled",
    "effect_days": "Effect days",
    "downtime_minutes": "Downtime minutes",
})
with st.expander("Each action"):
    st.dataframe(
    actions,
    hide_index=True,
    width="stretch",
    alt="July actions with source, destination, cost, and theoretical-win cases",
    column_config={
        "Cost": st.column_config.NumberColumn(format="dollar"),
        "Downtime cost": st.column_config.NumberColumn(format="dollar"),
        "Central benefit": st.column_config.NumberColumn(format="dollar"),
        "Low net": st.column_config.NumberColumn(format="dollar"),
        "Central net": st.column_config.NumberColumn(format="dollar"),
        "High net": st.column_config.NumberColumn(format="dollar"),
        "Scheduled": st.column_config.DateColumn(format="YYYY-MM-DD"),
    },
)
st.caption(
    "Benefit is gross incremental theoretical win over the effect days. "
    "Net subtracts the action cost and the downtime cost. "
    "The inspection's benefit is zero in every case: it buys a documented answer, not a guaranteed correction."
)

action_ranges = plan.copy()
action_ranges["label"] = action_ranges["target"].map(
    lambda machine: "Inspect M-1158" if machine == "M-1158" else f"Move {machine}"
)
st.plotly_chart(
    charts.range_bars(action_ranges, "label", "Net theoretical win by action", "net_low_usd", "net_central_usd", "net_high_usd"),
    width="stretch",
    theme="streamlit",
    alt="Low, central, and high net theoretical win for each July action",
)
weak_low = moves.loc[moves["net_low_usd"].idxmin()]
st.caption(
    f"The package stays positive in the low case, at {money(summary['net_low'])}. "
    f"{weak_low['target']}'s own low case is {money(weak_low['net_low_usd'])}. "
    "The inspection is negative in every case because its priced benefit is zero."
)

st.subheader("What the move math assumes")
st.markdown(
    f"**Low.** {move_rule['low_assumptions']}. "
    f"**Central.** {move_rule['central_assumptions']}. "
    f"**High.** {move_rule['high_assumptions']}."
)
st.markdown(
    "A move can displace play at the destination. The published break-even is the destination cannibalization "
    f"that erases the gain on the weakest move, {percent(move_rule['action_break_even'])}. "
    "Past relocations are a four-machine comparison with peers. They support trying a small set of moves. "
    "They do not guarantee that these three machines will match any one of those results."
)
with st.expander("Move factors published with the plan"):
    factor_table = moves.loc[:, [
        "target", "to_zone", "machine_factor", "game_appeal_factor",
        "target_break_even_cannibalization", "central_assumptions",
    ]].rename(columns={
        "target": "Machine",
        "to_zone": "To zone",
        "machine_factor": "Machine factor",
        "game_appeal_factor": "Game-appeal factor",
        "target_break_even_cannibalization": "Break-even cannibalization",
        "central_assumptions": "Central case",
    })
    st.dataframe(
        factor_table,
        hide_index=True,
        width="stretch",
        alt="Published move factors and the cannibalization that would erase each move",
        column_config={
            "Machine factor": st.column_config.NumberColumn(format="%.2f"),
            "Game-appeal factor": st.column_config.NumberColumn(format="%.2f"),
            "Break-even cannibalization": st.column_config.NumberColumn(format="percent"),
        },
    )
    st.caption("Factors are inputs to the scenario, not observed July results. Cannibalization is the share of the destination gain that would erase that move.")

st.divider()
st.subheader("Alternatives that were not funded")
alternatives = scenarios.loc[
    (scenarios["question"] == "Q6")
    & (scenarios["scenario_group"] == "budget alternatives (net theo over the plan horizon)")
].copy()
short_names = {
    "recommended": "Recommended",
    "do nothing": "Do nothing",
    "recommended plus one promotion weekend": "Add one promotion weekend",
    "replace the original bottom 10 by lease (2 already retired)": "Lease the original bottom 10",
    "replace the original bottom 10 by purchase (management's plan)": "Buy the original bottom 10",
    "best plan spending at least 90% of the budget": "Force at least 90% spent",
}
alternatives["label"] = alternatives["item"].map(short_names).fillna(alternatives["item"])
chartable = alternatives.loc[alternatives["value_central_usd"].notna()].copy()
#recommended at the end of the frame so the category array can place it last, which the chart draws on top
chartable["_order"] = chartable["item"].map({"recommended": 0}).fillna(1)
chartable = chartable.sort_values(["_order", "value_central_usd"], ascending=[True, True])
st.plotly_chart(
    charts.range_bars(
        chartable,
        "label",
        "Net theoretical win of the budget alternatives",
        "value_low_usd",
        "value_central_usd",
        "value_high_usd",
    ),
    width="stretch",
    theme="streamlit",
    alt="Low, central, and high net theoretical win for the recommended plan and the documented alternatives",
)

alt_table = alternatives.loc[:, ["label", "cost_usd", "value_low_usd", "value_central_usd", "value_high_usd", "feasible", "in_recommended_plan", "assumptions"]].copy()
alt_table = alt_table.rename(columns={
    "label": "Alternative",
    "cost_usd": "Cost",
    "value_low_usd": "Low net",
    "value_central_usd": "Central net",
    "value_high_usd": "High net",
    "feasible": "Fits the budget",
    "in_recommended_plan": "Funded",
    "assumptions": "Why it is or is not the plan",
})
st.dataframe(
    alt_table,
    hide_index=True,
    width="stretch",
    alt="Budget alternatives with cost and net theoretical-win cases",
    column_config={
        "Cost": st.column_config.NumberColumn(format="dollar"),
        "Low net": st.column_config.NumberColumn(format="dollar"),
        "Central net": st.column_config.NumberColumn(format="dollar"),
        "High net": st.column_config.NumberColumn(format="dollar"),
    },
)

maintenance = scenarios.loc[
    (scenarios["question"] == "Q3") & scenarios["scenario_group"].astype(str).str.startswith("maintenance")
].copy()
maintenance_cost = sorted({float(cost) for cost in maintenance["cost_usd"]})
staffing_best = float(maintenance["value_central_usd"].max())
st.markdown(
    "Replacing the original bottom ten does not follow from the location evidence: none of those machines are in the "
    "demand-adjusted bottom ten. Buying eight of them costs "
    f"**{money(alternatives.loc[alternatives['item'].str.startswith('replace the original bottom 10 by purchase'), 'cost_usd'].iloc[0])}**, "
    "which is over the budget. Leasing them has a negative net in every published case. "
    f"Forcing the plan to spend at least 90% of the budget is negative in every case. "
    f"Maintenance options priced elsewhere cost { ' or '.join(money(cost) for cost in maintenance_cost) }. "
    f"The largest staffing case is **{money(staffing_best)}** of theoretical win before its wage cost, so it does not clear that cost."
)

promo = scenarios.loc[(scenarios["question"] == "Q6") & (scenarios["item"] == "A-PROMO") & (scenarios["kind"] == "in plan net (central)")]
events = evidence.loc[evidence["question"] == "Q6"].copy()
if not promo.empty:
    promo_row = promo.iloc[0]
    st.caption(
        f"The promotion's central case transfers a historical association ({promo_row['assumptions']}). "
        "Gold labels that association as not causal. The low case assumes no uplift. "
        "A hypothetical recovery from the M-1158 inspection is also published, and it is not in the funded benefit."
    )
if not events.empty:
    during = events.loc[events["metric"].astype(str).str.contains("(during)", regex=False)].copy()
    during["label"] = during["metric"].astype(str).str.replace(" (during)", "", regex=False)
    st.plotly_chart(
        charts.effect_bars(during, "Historical event associations used only as a sensitivity", "label"),
        width="stretch",
        theme="streamlit",
        alt="Relative change in play during two past events, with intervals. Not a causal effect.",
    )
    st.caption(str(during["causal_interpretation"].iloc[0]).capitalize() + ". These associations are not a reason to fund a promotion.")

with st.expander("Inspection sensitivity that is not in the plan"):
    recovery = scenarios.loc[
        (scenarios["question"] == "Q6")
        & (scenarios["item"] == "A-INSPECT")
        & scenarios["scenario"].astype(str).str.startswith("hypothetical")
    ]
    st.markdown(
        "If a later, separately priced correction restored part of M-1158's theoretical win, Gold's sensitivity cases put that "
        f"gross benefit at {money(recovery.loc[recovery['scenario'].str.contains('low'), 'value_central_usd'].iloc[0])}, "
        f"{money(recovery.loc[recovery['scenario'].str.contains('central'), 'value_central_usd'].iloc[0])}, and "
        f"{money(recovery.loc[recovery['scenario'].str.contains('high'), 'value_central_usd'].iloc[0])} "
        "before any correction cost. The funded plan uses **zero**. The inspection does not establish a defect, and it does not promise that recovery."
    )

st.divider()
st.subheader("Thirty days, then a decision")
schedule = []
for _, row in plan.iterrows():
    schedule.append({
        "When": pd.Timestamp(row["scheduled_date"]).date(),
        "Action": f"{row['action']}: {row['target']}",
        "Measure": row["success_metric"],
        "Review": row["decision_rule"],
    })
st.dataframe(
    pd.DataFrame(schedule),
    hide_index=True,
    width="stretch",
    alt="July schedule with the published success measure and review rule for each action",
    column_config={"When": st.column_config.DateColumn(format="YYYY-MM-DD")},
)
st.markdown(
    f"Orders go in on July 1. The inspection is scheduled for {pd.Timestamp(inspection.iloc[0]['scheduled_date']).date()}, "
    f"and the three moves for {pd.Timestamp(moves.iloc[0]['scheduled_date']).date()}. "
    f"Move results are read over {int(move_rule['effect_days'])} days against the {int(move_rule['effect_days'])} days before, "
    "using theoretical win for the moved machines and the neighbouring machines at both the old and new banks. "
    f"Control: {move_rule['control']}."
)
st.markdown(
    f"**Stop or send back.** {move_rule['decision_rule']} "
    f"For the inspection: {inspection.iloc[0]['decision_rule']}"
)
st.caption(f"Confounders to hold in mind: {move_rule['confounders']}")

st.subheader("Why the other $93,400 stays unspent")
st.markdown(
    f"The catalog treats doing nothing as a valid recommendation. Spending the balance to look fully allocated "
    f"is the 90% alternative, and its central net is **{money(alternatives.loc[alternatives['item'].str.contains('90%'), 'value_central_usd'].iloc[0])}**. "
    "The rejected replacements, the promotion, and the maintenance options are either over budget, negative after cost, "
    "or built on an association that is not causal. "
    "The decision is to test location on three machines and to find out whether M-1158 matches its published math. "
    "A correction, a wider set of moves, or more comp spending would be a later decision with its own cost."
)
