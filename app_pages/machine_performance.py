#q1. six-month rankings stay fixed. date, zone, and game filters apply only below them.
from __future__ import annotations

import pandas as pd
import streamlit as st

from ante import charts, metrics
from ante.ui import frame, money, number, percent

scorecard = frame("gold_machine_scorecard")
bottom = frame("gold_bottom_ten")
daily = frame("gold_machine_daily")
evidence = frame("gold_evidence")

try:
    original = metrics.list_named(bottom, "original")
    exposure = metrics.list_named(bottom, "exposure")
    demand = metrics.list_named(bottom, "demand")
    actual = metrics.list_named(bottom, "actual")
except ValueError as exc:
    st.error(str(exc), title="Bottom-ten lists could not be read")
    st.stop()

lists = {
    "Raw house win": original,
    "Theo per available hour": exposure,
    "Demand index": demand,
    "Actual-win index": actual,
}

shared_demand = set(original["machine_id"]) & set(demand["machine_id"])
shared_exposure = set(original["machine_id"]) & set(exposure["machine_id"])
shared_actual = set(original["machine_id"]) & set(actual["machine_id"])
ranks = original.merge(
    scorecard.loc[:, ["machine_id", "rank_theo_per_hour", "rank_demand_index", "rank_actual_win_index"]],
    on="machine_id",
    how="left",
)

st.header("Raw earnings are the wrong replacement list.")
st.markdown(
    f"**{len(shared_demand)} of 10** machines with the lowest house win are in the demand bottom ten. "
    f"**{len(shared_exposure)} of 10** are still among the weakest on theoretical win per hour. "
    "The six-month lists below do not move when the date, zone, or game filters change."
)
with st.grid(3, gap="medium", min_column_width=180):
    st.metric("In the demand bottom ten", f"{len(shared_demand)} of 10", border=True, height="stretch", help="Original bottom ten that also appear on the demand-index bottom ten. Rank 1 is the weakest demand.")
    st.metric("Weak per available hour", f"{len(shared_exposure)} of 10", border=True, height="stretch", help="Also among the ten lowest on theoretical win per available hour.")
    st.metric("Weak on actual win", f"{len(shared_actual)} of 10", border=True, height="stretch", help="Also among the ten lowest actual-win indexes. Actual win includes luck.")
st.plotly_chart(
    charts.demand_ranks(ranks),
    width="stretch",
    theme="streamlit",
    alt="Demand rank of the ten machines with the lowest house win. The dotted line marks the bottom ten.",
)
st.caption("Dotted line: the demand bottom ten. Rank 1 is the weakest. A bar to the right of the line is not a low-demand machine.")

featured = scorecard.loc[scorecard["machine_id"] == "M-1158"]
if featured.empty:
    st.error("M-1158 is not on the machine scorecard. No inspection claim is shown in its place.", title="Missing machine")
else:
    machine = featured.iloc[0]
    lead = evidence.loc[
        (evidence["question"] == "Q1")
        & (evidence["subgroup"] == "M-1158")
        & (evidence["metric"].astype(str).str.startswith("reconciliation"))
    ]
    hit_row = evidence.loc[
        (evidence["subgroup"] == "M-1158")
        & (evidence["metric"].astype(str).str.startswith("hit-rate z"))
    ]
    luck_row = evidence.loc[
        (evidence["subgroup"] == "M-1158")
        & (evidence["metric"].astype(str).str.startswith("luck z"))
    ]
    problems = []
    if lead.empty or hit_row.empty or luck_row.empty:
        problems.append("gold_evidence is missing the M-1158 hit z, luck z, or reconciliation row.")
    else:
        if abs(float(machine["hit_z"]) - float(hit_row["estimate"].iloc[0])) > 1e-6:
            problems.append("The scorecard hit z does not match the evidence row.")
        if abs(float(machine["luck_z"]) - float(luck_row["estimate"].iloc[0])) > 1e-6:
            problems.append("The scorecard luck z does not match the evidence row.")
    st.subheader("M-1158 pays too often for its published math")
    st.caption(str(machine["scope"]))
    if problems:
        st.error(" ".join(problems) + " The figures below are the scorecard values, and they are not offered as a reconciled claim.", title="M-1158 is not reconciled")
    st.markdown(
        f"{machine['theme']} in {machine['primary_zone']}. "
        "The hit rate is above the published probability, and the machine kept less than its theoretical win. "
        "Inspect the configuration. This is not a finding of fraud or of a broken machine."
    )
    with st.grid(4, gap="medium"):
        st.metric("Hit rate", number(machine["hit_rate"], 4), help=f"Published hit probability {number(machine['hit_probability'], 4)}. Share of spins that paid.", border=True, height="stretch")
        st.metric("Hit z", number(machine["hit_z"], 2), help="Binomial test of the hit rate against the published hit probability. A large z means the frequency gap is very unlikely if the published probability is right.", border=True, height="stretch")
        st.metric("Observed hold", percent(machine["observed_hold"], 2), help=f"Theoretical hold {percent(machine['theo_hold'], 0)}. House win divided by coin-in.", border=True, height="stretch")
        st.metric("Luck z", number(machine["luck_z"], 2), help=f"House win versus theoretical win. Bonferroni p across 310 machines: {number(machine['luck_p_bonferroni'], 4)}.", border=True, height="stretch")
    st.markdown(
        "These are different tests. **Hit z** asks how often spins pay. Each spin is close to a yes or no, "
        "so a gap of about seven points becomes a very large z. **Luck z** asks about the money. Payout sizes vary, "
        "so the same pattern is noisier and the z is smaller. Both point the same way: the payout pattern does not "
        "match the published configuration. The July inspection is priced as information, with no assumed recovery."
    )
    if not lead.empty and not problems:
        with st.expander("Reconciliation note from the Gold evidence table"):
            st.markdown(str(lead["notes"].iloc[0]))
            st.caption(f"{lead['evidence_class'].iloc[0]} · {lead['causal_interpretation'].iloc[0]}")

flagged = scorecard.loc[scorecard["requires_investigation"] == True, ["machine_id", "review_notes"]]  # noqa: E712
others = flagged.loc[flagged["machine_id"] != "M-1158"]
if not others.empty:
    names = ", ".join(others["machine_id"].tolist())
    st.caption(
        f"Also flagged for investigation, for fault rates rather than payouts: {names}. "
        "Fault detail is on Downtime & operations. A flag is a statistical lead, not a malfunction finding."
    )

st.subheader("The published lists")
st.caption(str(original["scope"].iloc[0]))

choice = st.segmented_control(
    "Published list",
    options=list(lists),
    default="Raw house win",
    key="published_list",
)
if choice is None:
    st.info("Choose a published list.", title="No list selected")
else:
    shown = lists[choice]
    table = shown.loc[:, [
        "list_rank", "machine_id", "primary_zone", "theme", "metric_name", "metric_value",
        "house_win_usd", "theo_usd", "luck_residual_usd", "theo_per_avail_hour",
        "demand_index", "actual_win_index", "in_original_bottom_10", "lifecycle_status",
        "requires_investigation",
    ]].rename(columns={
        "list_rank": "Rank",
        "machine_id": "Machine",
        "primary_zone": "Zone",
        "theme": "Game",
        "metric_name": "Ranked by",
        "metric_value": "Value",
        "house_win_usd": "House win",
        "theo_usd": "Theo",
        "luck_residual_usd": "Luck residual",
        "theo_per_avail_hour": "Theo per hour",
        "demand_index": "Demand index",
        "actual_win_index": "Actual-win index",
        "in_original_bottom_10": "Also raw bottom ten",
        "lifecycle_status": "Lifecycle",
        "requires_investigation": "Investigate",
    })
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        alt=f"Published six-month bottom ten ranked by {choice}",
        column_config={
            "Rank": st.column_config.NumberColumn(help="1 is the weakest on this list."),
            "House win": st.column_config.NumberColumn(format="dollar"),
            "Theo": st.column_config.NumberColumn(format="dollar"),
            "Luck residual": st.column_config.NumberColumn(format="dollar", help="House win minus theoretical win."),
            "Theo per hour": st.column_config.NumberColumn(format="$%.2f"),
            "Demand index": st.column_config.NumberColumn(format="%.2f"),
            "Actual-win index": st.column_config.NumberColumn(format="%.2f"),
            "Value": st.column_config.NumberColumn(format="%.2f"),
        },
    )

rank_view = ranks.loc[:, [
    "list_rank", "machine_id", "primary_zone", "house_win_usd", "theo_usd",
    "rank_theo_per_hour", "rank_demand_index", "rank_actual_win_index", "days_on_floor",
]].rename(columns={
    "list_rank": "Raw rank",
    "machine_id": "Machine",
    "primary_zone": "Zone",
    "house_win_usd": "House win",
    "theo_usd": "Theo",
    "rank_theo_per_hour": "Theo-per-hour rank",
    "rank_demand_index": "Demand rank",
    "rank_actual_win_index": "Actual-win rank",
    "days_on_floor": "Days on floor",
})
with st.expander("Where the raw bottom ten land on the other ranks"):
    st.caption("Rank 1 is the weakest of 310 machines. A large rank means the machine is not weak on that measure.")
    st.dataframe(
    rank_view,
    hide_index=True,
    width="stretch",
    alt="Raw bottom ten with their ranks on theoretical win per hour, demand, and actual win",
    column_config={
        "House win": st.column_config.NumberColumn(format="dollar"),
        "Theo": st.column_config.NumberColumn(format="dollar"),
    },
)

with st.expander("House win, theoretical win, and the two indexes"):
    st.plotly_chart(
        charts.house_versus_theo(original),
        width="stretch",
        theme="streamlit",
        alt="Observed house win and theoretical win for the ten machines with the lowest house win",
    )
    st.caption("Theoretical win is not added to house win. Where the gold bar is far to the right of the ivory bar, the machine kept less than the published math expected.")
    st.plotly_chart(
        charts.demand_scatter(scorecard),
        width="stretch",
        theme="streamlit",
        alt="Each machine as demand index against actual-win index, with the raw bottom ten marked",
    )
    st.caption(
        "Demand index compares play with zone and game peers and leaves luck out. "
        "Actual-win index includes luck. M-1158 is weak in the money result, not in demand."
    )

st.subheader("Performance for the selected days")
st.caption(
    "Sums of on-floor machine-days. Hold and theo per hour are ratios of those sums. "
    "Luck z and hit z here are recomputed from the selected days. They are not the published six-month scores. "
    "The demand index is not recomputed, because it is a fixed model, not a daily sum."
)

zones = sorted(daily["zone_name"].dropna().unique().tolist())
themes = sorted(daily["theme"].dropna().unique().tolist())
with st.container(border=True):
    date_choice = st.date_input(
        "Gaming dates",
        value=(daily["gaming_date"].min().date(), daily["gaming_date"].max().date()),
        min_value=daily["gaming_date"].min().date(),
        max_value=daily["gaming_date"].max().date(),
        format="YYYY-MM-DD",
        key="machine_dates",
        help="Applies to this section only.",
    )
    zone_choice = st.pills(
        "Zones",
        options=zones,
        selection_mode="multi",
        default=[],
        key="machine_zones",
        help="Leave unselected to include every zone. A machine that moved is counted only on days it sat in a selected zone.",
    )
    theme_choice = st.multiselect(
        "Games",
        options=themes,
        default=[],
        key="machine_themes",
        help="Leave empty to include every game. Game is the theme on that machine-day.",
        placeholder="All games",
    )
    search = st.text_input(
        "Machine search",
        value="",
        key="machine_search",
        placeholder="M-1158",
        help="Filters the ranking to machine ids containing this text. It does not change the six-month lists.",
    )

if not (isinstance(date_choice, tuple) and len(date_choice) == 2 and date_choice[0] and date_choice[1]):
    st.info("Select a start date and an end date.", title="Date range incomplete")
    st.stop()

selected = metrics.between_dates(daily, date_choice[0], date_choice[1])
full_window = (
    date_choice[0] == daily["gaming_date"].min().date()
    and date_choice[1] == daily["gaming_date"].max().date()
    and not zone_choice
    and not theme_choice
    and not (search or "").strip()
)
ranked = metrics.period_machines(selected, zone_choice or None, theme_choice or None, search)

if ranked.empty:
    st.warning("No on-floor machine-days match these filters.", title="Empty selection")
else:
    coin = float(ranked["coin_in_usd"].sum())
    house = float(ranked["house_win_usd"].sum())
    theo = float(ranked["theo_usd"].sum())
    with st.grid(4, gap="medium"):
        st.metric("Machines", f"{ranked['machine_id'].nunique():,}", help="Distinct machines with at least one on-floor day in the selection.", border=True, height="stretch")
        st.metric("Coin-in", money(coin), help="Observed. Sum of machine-days in the selection.", border=True, height="stretch")
        st.metric("House win", money(house), help="Observed.", border=True, height="stretch")
        st.metric("Theo per hour", money(metrics.ratio(theo, ranked["available_hours"].sum()), 2), help="Theoretical win divided by available hours, both summed. Not an average of daily rates.", border=True, height="stretch")

    filtered_days = selected
    if zone_choice:
        filtered_days = filtered_days.loc[filtered_days["zone_name"].isin(zone_choice)]
    if theme_choice:
        filtered_days = filtered_days.loc[filtered_days["theme"].isin(theme_choice)]
    if (search or "").strip():
        filtered_days = filtered_days.loc[filtered_days["machine_id"].str.contains(search.strip(), case=False, na=False)]
    st.plotly_chart(
        charts.money_lines(
            metrics.daily_totals(filtered_days),
            "Selected days: house win and theoretical win",
            [
                ("house_win_usd", "Observed house win", charts.IVORY),
                ("theo_usd", "Theoretical win", charts.GOLD),
            ],
        ),
        width="stretch",
        theme="streamlit",
        alt="Daily observed house win and theoretical win for the current machine filters",
    )

    weakest = set(ranked.nsmallest(10, "house_win_usd")["machine_id"])
    if full_window:
        published = set(original["machine_id"])
        if weakest == published:
            st.caption("With the full window and no zone, game, or machine filter, the ten lowest house-win machines match the published raw bottom ten.")
        else:
            st.error(
                "The recomputed bottom ten does not match the published list: "
                f"only in the recompute {sorted(weakest - published) or 'none'}; "
                f"only in the published list {sorted(published - weakest) or 'none'}.",
                title="Ranking check failed",
            )

    view = ranked.loc[:, [
        "rank_in_selection", "machine_id", "zone_name", "theme", "days",
        "coin_in_usd", "house_win_usd", "theo_usd", "observed_hold",
        "theo_per_available_hour", "luck_z_in_selection", "hit_z_in_selection",
    ]].rename(columns={
        "rank_in_selection": "Rank",
        "machine_id": "Machine",
        "zone_name": "Zone with most hours",
        "theme": "Game with most hours",
        "days": "Days",
        "coin_in_usd": "Coin-in",
        "house_win_usd": "House win",
        "theo_usd": "Theo",
        "observed_hold": "Hold",
        "theo_per_available_hour": "Theo per hour",
        "luck_z_in_selection": "Luck z in selection",
        "hit_z_in_selection": "Hit z in selection",
    })
    st.dataframe(
        view,
        hide_index=True,
        width="stretch",
        alt="Machines in the current selection, weakest house win first. This is not the published six-month ranking.",
        column_config={
            "Rank": st.column_config.NumberColumn(help="1 is the lowest house win inside this selection only."),
            "Coin-in": st.column_config.NumberColumn(format="dollar"),
            "House win": st.column_config.NumberColumn(format="dollar"),
            "Theo": st.column_config.NumberColumn(format="dollar"),
            "Hold": st.column_config.NumberColumn(format="percent"),
            "Theo per hour": st.column_config.NumberColumn(format="$%.2f"),
            "Luck z in selection": st.column_config.NumberColumn(format="%.2f", help="Recomputed from selected days. Not the six-month luck z."),
            "Hit z in selection": st.column_config.NumberColumn(format="%.2f", help="Recomputed from selected days. Not the six-month hit z."),
        },
    )

st.subheader("One machine")
st.caption("The chart uses the date range above and ignores the zone and game filters, so a machine's own history stays visible when you are looking at another part of the floor.")
machine_ids = scorecard["machine_id"].sort_values().tolist()
default_index = machine_ids.index("M-1158") if "M-1158" in machine_ids else 0
picked = st.selectbox(
    "Machine",
    options=machine_ids,
    index=default_index,
    key="machine_detail",
    placeholder="Choose a machine",
)
card = scorecard.loc[scorecard["machine_id"] == picked].iloc[0]
history = metrics.between_dates(daily.loc[daily["machine_id"] == picked], date_choice[0], date_choice[1])
with st.container(border=True):
    st.markdown(f"**{picked}** · {card['theme']} · {card['primary_zone']} · {card['lifecycle_status']}")
    st.caption("Six-month scorecard. These figures ignore the date filter.")
    with st.grid(4, gap="medium"):
        st.metric("House win", money(card["house_win_usd"]), border=True, height="stretch", help="Observed, full window.")
        st.metric("Theo", money(card["theo_usd"]), border=True, height="stretch", help="Theoretical, full window.")
        st.metric("Demand index", number(card["demand_index"], 2), border=True, height="stretch", help="Fixed. Below 1 means less play than zone and game peers.")
        st.metric("Published luck z", number(card["luck_z"], 2), border=True, height="stretch", help="Fixed six-month test. The chart below can cover a shorter range.")
    if bool(card["requires_investigation"]):
        st.info(str(card["review_notes"]), title="Requires investigation", icon=":material/search:")
    if history.empty:
        st.warning(f"{picked} has no on-floor days in the selected dates.", title="No days in range")
    else:
        st.plotly_chart(
            charts.money_lines(
                history.sort_values("gaming_date"),
                f"{picked} on the selected dates",
                [
                    ("house_win_usd", "Observed house win", charts.IVORY),
                    ("theo_usd", "Theoretical win", charts.GOLD),
                ],
            ),
            width="stretch",
            theme="streamlit",
            alt=f"Daily observed house win and theoretical win for {picked}",
        )
        st.caption(
            f"In this date range: house win {money(history['house_win_usd'].sum())}, "
            f"theo {money(history['theo_usd'].sum())}, "
            f"luck z {number(metrics.z_score(history['luck_residual_usd'].sum(), history['luck_variance_usd2'].sum()), 2)}. "
            "The luck z is recomputed for these days only."
        )
