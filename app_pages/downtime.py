#q3. observed hours stay separate from estimated lost play. maintenance scenarios are not the july plan.
from __future__ import annotations

import pandas as pd
import streamlit as st

from ante import charts, metrics
from ante.ui import frame, money, number, percent, signed_percent

tickets = frame("gold_downtime_tickets")
zones = frame("gold_zone_daily")
property_daily = frame("gold_property_daily")
evidence = frame("gold_evidence")
scenarios = frame("gold_scenarios")
plan = frame("gold_investment_plan")

CAUSE_COLUMNS = {
    "Fault repair": "downtime_fault_repair_hours",
    "Scheduled maintenance": "downtime_scheduled_maintenance_hours",
    "Move": "downtime_move_hours",
    "Install": "downtime_install_hours",
    "Retire": "downtime_retire_hours",
}
CAUSE_ORDER = {name: index for index, name in enumerate(CAUSE_COLUMNS)}

first_day = property_daily["gaming_date"].min().date()
last_day = property_daily["gaming_date"].max().date()
zone_names = sorted(zones["zone_name"].dropna().unique().tolist())

try:
    gross_coin = metrics.one_row(scenarios, question="Q3", scenario="zone x month x weekday x hour, machine-scaled", item="gross lost coin-in")
    gross_theo = metrics.one_row(scenarios, question="Q3", scenario="zone x month x weekday x hour, machine-scaled", item="gross lost expected win (theo)")
    net_theo = metrics.one_row(scenarios, question="Q3", scenario="gross x (1 - displacement share)", item="net lost expected win (theo)")
    displacement = metrics.one_row(evidence, question="Q3", topic="lost-play model", metric="displacement_share")
    plan_summary = metrics.plan_summary(plan)
except ValueError as exc:
    st.error(str(exc), title="Downtime evidence is missing")
    st.stop()

st.header("The floor stayed available. The large maintenance spends stayed out.")
st.markdown(
    f"The funded plan spends **{money(plan_summary['spend'])}**. "
    "Preventive maintenance and a technician shift were priced, and neither cleared its cost."
)

with st.container(border=True):
    date_choice = st.date_input(
        "Gaming dates",
        value=(first_day, last_day),
        min_value=first_day,
        max_value=last_day,
        format="YYYY-MM-DD",
        key="downtime_dates",
        help="Filters observed availability, hours, and the gross ticket estimate. The published whole-window estimate does not change.",
    )
    zone_choice = st.pills(
        "Zones",
        options=zone_names,
        selection_mode="multi",
        default=[],
        key="downtime_zones",
        help="Leave unselected for the whole floor. A zone filter uses zone-days and tickets opened in those zones.",
    )

if not (isinstance(date_choice, tuple) and len(date_choice) == 2 and date_choice[0] and date_choice[1]):
    st.info("Select a start date and an end date.", title="Date range incomplete")
    st.stop()

full_window = date_choice[0] == first_day and date_choice[1] == last_day and not zone_choice
zone_view = metrics.between_dates(zones, date_choice[0], date_choice[1])
property_view = metrics.between_dates(property_daily, date_choice[0], date_choice[1])
if zone_choice:
    zone_view = zone_view.loc[zone_view["zone_name"].isin(zone_choice)]
full_dates = date_choice[0] == first_day and date_choice[1] == last_day
opened = tickets["opened_gaming_date"]
dated = opened.notna() & (opened >= pd.Timestamp(date_choice[0])) & (opened <= pd.Timestamp(date_choice[1]))
#installs, moves, retirements, and scheduled work can have no opened gaming date.
#they belong to the january-june window, so they stay in view only while that whole window is selected.
ticket_view = tickets.loc[dated | (opened.isna() & full_dates)].copy()
if zone_choice:
    ticket_view = ticket_view.loc[ticket_view["zone_name"].isin(zone_choice)]
undated_count = int(tickets["opened_gaming_date"].isna().sum())
if zone_view.empty:
    st.warning("No zone-days match these filters.", title="Empty selection")
    st.stop()

available = float(zone_view["available_hours"].sum())
scheduled = float(zone_view["scheduled_machine_hours"].sum())
downtime_hours = float(zone_view["downtime_hours"].sum())
faults = float(zone_view["faults_opened"].sum())
if not zone_choice:
    property_gap = abs(downtime_hours - float(property_view["downtime_hours"].sum()))
    if property_gap > 0.05:
        st.error(
            f"Zone downtime and the property total differ by {property_gap:,.2f} hours for these dates.",
            title="Downtime hours do not reconcile",
        )

with st.grid(3, gap="medium", min_column_width=180):
    st.metric(
        "Availability",
        percent(metrics.ratio(available, scheduled), 2),
        border=True,
        height="stretch",
        help="Available machine-hours divided by scheduled machine-hours.",
    )
    st.metric(
        "Downtime",
        f"{downtime_hours:,.0f} h",
        border=True,
        height="stretch",
        help="Scheduled hours minus available hours. Not an estimate.",
    )
    st.metric(
        "Faults opened",
        f"{faults:,.0f}",
        border=True,
        height="stretch",
        help="Faults opened on machine-days in the selection. Moves and installs are separate tickets.",
    )

if zone_choice:
    cause_hours = (
        ticket_view.groupby("cause", as_index=False)["total_hours"].sum()
        .rename(columns={"cause": "label", "total_hours": "hours"})
    )
    cause_hours["label"] = cause_hours["label"].map({
        "fault_repair": "Fault repair",
        "scheduled_maintenance": "Scheduled maintenance",
        "move": "Move",
        "install": "Install",
        "retire": "Retire",
    }).fillna(cause_hours["label"])
    cause_note = "Hours on tickets in the selected zones."
    if full_dates:
        cause_note += " The full date window includes tickets that have no opened gaming date."
        ticket_hours = float(ticket_view["total_hours"].sum())
        if abs(ticket_hours - downtime_hours) > 0.05:
            st.error(
                f"Ticket hours ({ticket_hours:,.1f}) do not match zone downtime ({downtime_hours:,.1f}).",
                title="Ticket hours do not reconcile",
            )
    else:
        cause_note += " A shorter date range counts tickets by the day they opened, so it can differ from downtime hours on those days."
else:
    cause_hours = pd.DataFrame({
        "label": list(CAUSE_COLUMNS),
        "hours": [float(property_view[column].sum()) for column in CAUSE_COLUMNS.values()],
    })
    cause_note = "Property totals by cause. Waiting time is not included, because it sits inside the repair window."
    if full_window:
        ticket_hours = float(tickets["total_hours"].sum())
        if abs(ticket_hours - downtime_hours) > 0.05:
            st.error(
                f"Ticket hours ({ticket_hours:,.1f}) do not match property downtime ({downtime_hours:,.1f}).",
                title="Ticket hours do not reconcile",
            )
cause_hours["order"] = cause_hours["label"].map(CAUSE_ORDER).fillna(9)
cause_hours = cause_hours.sort_values("hours")
st.plotly_chart(
    charts.cause_bars(cause_hours["label"].tolist(), cause_hours["hours"].tolist(), "Downtime by cause"),
    width="stretch",
    theme="streamlit",
    alt="Observed downtime hours by cause for the current filters",
)
st.caption(cause_note)

with st.expander("Hours by weekday and zone"):
    by_day = zone_view.groupby("local_weekday", as_index=False)["downtime_hours"].sum()
    by_day["local_weekday"] = pd.Categorical(by_day["local_weekday"], charts.WEEKDAY_ORDER, ordered=True)
    by_day = by_day.sort_values("local_weekday")
    st.plotly_chart(
        charts.cause_bars(
            by_day["local_weekday"].astype(str).tolist(),
            by_day["downtime_hours"].tolist(),
            "Downtime by weekday",
        ),
        width="stretch",
        theme="streamlit",
        alt="Observed downtime hours by weekday for the current filters",
    )
    by_zone = metrics.zone_rates(zone_view, ["zone_name"])
    st.plotly_chart(
        charts.value_bars(by_zone, "zone_name", "downtime_hours", "Downtime hours by zone", "Hours"),
        width="stretch",
        theme="streamlit",
        alt="Observed downtime hours by zone for the selected dates",
    )

st.subheader("What the lost time might have been worth")
gross_selection_coin = float(ticket_view["est_gross_lost_coin_in_usd"].sum())
gross_selection_theo = float(ticket_view["est_gross_lost_theo_usd"].sum())
with st.grid(2, gap="medium", min_column_width=220):
    st.metric(
        "Gross coin-in in view",
        money(gross_selection_coin),
        border=True,
        height="stretch",
        help="Sum of the ticket-level gross estimates for the current filters. Guests may have played another machine.",
    )
    st.metric(
        "Gross theo in view",
        money(gross_selection_theo),
        border=True,
        height="stretch",
        help="Theoretical win, not cash the casino kept. Same gross method, same displacement warning.",
    )
undated_note = (
    f"{undated_count} tickets have no opened gaming date. They stay in these sums only while the date range is the full January–June window, and they are part of the published total."
    if full_dates
    else f"{undated_count} tickets have no opened gaming date, so a shorter date range leaves them out. The published total below still includes them."
)
st.caption(
    "These ticket sums are a gross estimate. They are not observed revenue, and they are not all retained. " + undated_note
)

st.markdown("**Published whole-window estimate.** Fixed. The filters above do not change it.")
if full_window:
    published_coin = float(tickets["est_gross_lost_coin_in_usd"].sum())
    published_theo = float(tickets["est_gross_lost_theo_usd"].sum())
    theo_gap = abs(published_theo - float(gross_theo["value_central_usd"]))
    coin_gap = abs(published_coin - float(gross_coin["value_central_usd"]))
    if theo_gap > 1 or coin_gap > 1:
        st.error(
            f"Ticket gross theo differs from the published estimate by {money(theo_gap)}, "
            f"and coin-in by {money(coin_gap)}.",
            title="Lost-play estimate does not reconcile",
        )
    else:
        st.caption("Ticket gross coin-in and theoretical win match the published zone-hour estimate, within one dollar.")
with st.grid(3, gap="medium", min_column_width=180):
    st.metric(
        "Gross coin-in",
        money(gross_coin["value_central_usd"]),
        border=True,
        height="stretch",
        help=f"Central estimate. Range {money(gross_coin['value_low_usd'])} to {money(gross_coin['value_high_usd'])}. Assumes nobody played elsewhere.",
    )
    st.metric(
        "Gross theo",
        money(gross_theo["value_central_usd"]),
        border=True,
        height="stretch",
        help=f"Range {money(gross_theo['value_low_usd'])} to {money(gross_theo['value_high_usd'])}.",
    )
    st.metric(
        "Net theo",
        money(net_theo["value_central_usd"]),
        border=True,
        height="stretch",
        help=(
            f"Gross theo after a displacement share of {signed_percent(displacement['estimate'])} "
            f"(interval {signed_percent(displacement['ci_low'])} to {signed_percent(displacement['ci_high'])}). "
            "The share was measured on neighbouring machines and applied to the whole floor. The interval includes zero."
        ),
    )

st.subheader("Priced, and not funded")
maintenance = scenarios.loc[
    (scenarios["question"] == "Q3") & scenarios["scenario_group"].astype(str).str.startswith("maintenance")
].copy()
best_benefit = float(maintenance["value_central_usd"].max())
st.markdown(
    f"The funded plan spends **{money(plan_summary['spend'])}** on an inspection and three relocations. "
    f"These maintenance actions cost {' or '.join(money(cost) for cost in sorted(set(float(cost) for cost in maintenance['cost_usd'])))}. "
    f"The largest theoretical-win case among the staffing options is **{money(best_benefit)}** before its cost. "
    "That does not clear the cost, so none of them is in the funded plan."
)
rows = []
for _, row in maintenance.sort_values(["item", "scenario"]).iterrows():
    if pd.notna(row["break_even"]):
        result = f"Break-even if {float(row['break_even']):.0%} of expected faults are prevented"
    elif pd.notna(row["value_central_usd"]):
        result = f"{money(row['value_central_usd'])} theo before the action cost"
    else:
        result = "—"
    rows.append({
        "Action": row["item"],
        "Case": row["scenario"],
        "Cost": row["cost_usd"],
        "Published result": result,
        "In the funded plan": "Yes" if bool(row["in_recommended_plan"]) else "No",
    })
st.dataframe(
    pd.DataFrame(rows),
    hide_index=True,
    width="stretch",
    alt="Maintenance and staffing scenarios that are not part of the funded July plan",
    column_config={"Cost": st.column_config.NumberColumn(format="dollar")},
)
st.caption(
    "A break-even above 100% means the action cannot pay for itself even if every expected fault in the window is prevented. "
    "Staffing results are theoretical win before the wage cost. They are scenarios, not observed savings."
)

rates = evidence.loc[
    (evidence["question"] == "Q3")
    & (evidence["metric"] == "faults per 1,000 machine-days")
].copy()
rates["label"] = rates["topic"].map({
    "fault rates by manufacturer": "Maker",
    "fault rates by machine age": "Age",
}).fillna("") + ": " + rates["subgroup"].astype(str)
st.plotly_chart(
    charts.interval_markers(rates, "Faults per 1,000 machine-days", "label", "Faults per 1,000 machine-days"),
    width="stretch",
    theme="streamlit",
    alt="Observed fault rates by manufacturer and machine age, with Poisson intervals",
)
st.caption("Descriptive rates with Poisson intervals. Age is confounded with manufacturer, so the age points are not a reason to retire older machines.")
