#q4 and q5. aggregates only. one guest attribute at a time. comps are counted once.
from __future__ import annotations

import pandas as pd
import streamlit as st

from ante import charts, metrics
from ante.ui import frame, money, percent, signed_percent

segments = frame("gold_player_segments")
property_daily = frame("gold_property_daily")
evidence = frame("gold_evidence")

MONTHS = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06"]
MONTH_LABELS = {
    "2025-01": "Jan",
    "2025-02": "Feb",
    "2025-03": "Mar",
    "2025-04": "Apr",
    "2025-05": "May",
    "2025-06": "Jun",
}
DIMENSIONS = {
    "Home region": "home_region",
    "Opening tier": "opening_tier",
    "Age band": "age_band",
    "Enrollment": "enrollment_cohort",
}
POLICY_METRICS = [
    ("points redeemed per day", "Points redeemed per day"),
    ("comp cost per day", "Comp cost per day"),
    ("carded coin-in per day", "Carded wagers per day"),
    ("carded theo per day", "Carded theoretical win per day"),
    ("comp cost per carded theo (reinvestment)", "Comp cost per carded theo"),
]

window = segments.loc[
    segments["period_type"].astype(str).str.startswith("window") & (segments["dimension"] == "all")
]
if len(window) != 1:
    st.error("The six-month player total is missing.", title="Player evidence is missing")
    st.stop()
window_row = window.iloc[0]
regions = segments.loc[
    segments["period_type"].astype(str).str.startswith("window") & (segments["dimension"] == "home_region")
]

try:
    top5 = metrics.one_row(
        evidence,
        question="Q4",
        topic="concentration of carded play",
        metric="share of coin_in_usd",
        subgroup="top 5% top wagerers (by coin-in)",
    )
    gini = metrics.one_row(
        evidence,
        question="Q4",
        topic="concentration of carded play",
        metric="gini of coin_in_usd",
    )
    local_comp = metrics.one_row(
        evidence,
        question="Q5",
        topic="local vs non-local comps",
        metric="local share of comp cost",
        subgroup="all players",
    )
    local_theo = metrics.one_row(
        evidence,
        question="Q5",
        topic="local vs non-local comps",
        metric="local share of carded theo",
        subgroup="all players",
    )
    points_raw = metrics.one_row(
        evidence,
        question="Q5",
        topic="may 1 comp price change",
        metric="points redeemed per day",
        subgroup="primary: feb-apr vs may-jun, raw ratio of sums",
    )
except ValueError as exc:
    st.error(str(exc), title="Player evidence is missing")
    st.stop()

carded_wagers = float(window_row["coin_in_usd"])
carded_payouts = float(window_row["payout_usd"])
player_net = float(window_row["net_usd"])
local_wagers = float(regions.loc[regions["segment"] == "Local", "coin_in_usd"].sum())
floor_wagers = float(property_daily["coin_in_usd"].sum())
comp_cost = float(property_daily["comp_cost_usd"].sum())
comp_face = float(property_daily["comp_face_value_usd"].sum())
pos_cogs = float(property_daily["pos_cogs_usd"].sum())
non_comp_cogs = float(property_daily["non_comp_cogs_usd"].sum())
segment_comp = float(window_row["comp_cost_usd"])

if abs(carded_wagers - 163_804_869.65) > 0.02 or abs(player_net + 15_585_460.60) > 0.02:
    st.error(
        "The six-month carded total does not match the validated player result. Showing the Gold value.",
        title="Carded play does not reconcile",
    )
if abs(comp_cost - 190_669.30) > 0.02 or abs(comp_cost - segment_comp) > 0.02:
    st.error(
        "Property comp cost and the player-segment comp cost do not both equal $190,669.30.",
        title="Comp cost does not reconcile",
    )
if abs((pos_cogs - comp_cost) - non_comp_cogs) > 0.05:
    st.error(
        "Comp cost is not the difference between F&B cost of goods and non-comp cost of goods.",
        title="Comp cost would be counted twice",
    )
if percent(metrics.ratio(local_wagers, carded_wagers)) != "58.6%":
    st.error("The local share of carded wagers does not round to 58.6%.", title="Local share does not reconcile")
if percent(top5["estimate"]) != "52.1%":
    st.error("The top 5% wagering share does not round to 52.1%.", title="Concentration does not reconcile")
if percent(metrics.ratio(carded_wagers, floor_wagers)) != "65.8%":
    st.error("Carded play is not 65.8% of machine coin-in.", title="Tracked share does not reconcile")

st.header("A few carded players place most of the wagers.")
st.markdown(
    f"The top 5% of wagerers account for **{percent(top5['estimate'])}** of carded coin-in. "
    f"Carded play is **{percent(metrics.ratio(carded_wagers, floor_wagers))}** of machine coin-in. "
    f"Comp cost is **{money(comp_cost, 2)}**, already inside food and beverage cost. No player records are on this page."
)

with st.container(border=True):
    month_choice = st.pills(
        "Months",
        options=MONTHS,
        format_func=lambda month: MONTH_LABELS[month],
        selection_mode="multi",
        default=MONTHS,
        key="player_months",
        help="Filters observed monthly totals. It does not change the six-month concentration or the May 1 comparison.",
    )
    dimension_label = st.segmented_control(
        "One guest attribute",
        options=list(DIMENSIONS),
        default="Home region",
        key="player_dimension",
        help="Each attribute is a complete partition. Attributes are not crossed, because those cells are not published.",
    )

months = list(month_choice or [])
full_months = months == MONTHS
dimension = DIMENSIONS.get(dimension_label or "Home region", "home_region")

st.subheader("Carded play")
if not months:
    st.info("Select at least one month to see observed play.", title="No months selected")
else:
    try:
        month_all, suppressed_all = metrics.monthly_segment_sums(segments, months, "all")
    except ValueError as exc:
        st.error(str(exc), title="Player months are missing")
        st.stop()
    if not suppressed_all.empty:
        st.warning(
            f"{len(suppressed_all)} segment cells are suppressed and left out of these sums.",
            title="Suppressed cells",
        )
    selected_wagers = float(month_all["coin_in_usd"].sum())
    selected_payouts = float(month_all["payout_usd"].sum())
    selected_net = float(month_all["net_usd"].sum())
    property_months = property_daily.loc[property_daily["gaming_date"].dt.strftime("%Y-%m").isin(months)]
    selected_floor = float(property_months["coin_in_usd"].sum())
    with st.grid(4, gap="medium", min_column_width=180):
        st.metric(
            "Carded wagers",
            money(selected_wagers),
            border=True,
            height="stretch",
            help="Coin-in on carded play. Wagering volume, not casino revenue.",
        )
        st.metric(
            "Carded payouts",
            money(selected_payouts),
            border=True,
            height="stretch",
            help="Amounts paid back to carded players. Not a loss and not a comp.",
        )
        st.metric(
            "Net player outcome",
            f"-{money(abs(selected_net))}" if selected_net < 0 else money(selected_net),
            border=True,
            height="stretch",
            help="Carded wagers minus carded payouts, from the player's side. The casino's house win on the same play is the opposite sign.",
        )
        st.metric(
            "Share of machine coin-in",
            percent(metrics.ratio(selected_wagers, selected_floor)),
            border=True,
            height="stretch",
            help="Carded wagers divided by all machine coin-in in the selected months. The rest is untracked play.",
        )
    if full_months:
        st.caption(
            f"{int(window_row['players_with_play']):,} distinct players had carded play across the six months. "
            "Player counts are not added across months, because the same person returns."
        )
    else:
        st.caption("These money totals add across the selected months. Distinct players are shown only for the full six-month window.")

    by_month = segments.loc[
        (segments["period_type"] == "month")
        & (segments["dimension"] == "all")
        & (segments["period_month"].isin(months))
    ].sort_values("period_month").copy()
    floor_by_month = (
        property_months.assign(period_month=property_months["gaming_date"].dt.strftime("%Y-%m"))
        .groupby("period_month", as_index=False)["coin_in_usd"].sum()
        .rename(columns={"coin_in_usd": "floor_coin_in_usd"})
    )
    by_month = by_month.merge(floor_by_month, on="period_month", how="left")
    by_month["Untracked wagers"] = by_month["floor_coin_in_usd"] - by_month["coin_in_usd"]
    by_month["label"] = by_month["period_month"].map(MONTH_LABELS)
    st.plotly_chart(
        charts.grouped_bars(
            by_month,
            "label",
            [
                ("coin_in_usd", "Carded wagers", charts.GOLD),
                ("Untracked wagers", "Untracked wagers", charts.SLATE),
            ],
            "Carded and untracked wagers",
            "USD",
        ),
        width="stretch",
        theme="streamlit",
        alt="Monthly carded wagers and untracked machine coin-in",
    )
    st.caption("Both series are wagering volume. Untracked wagers are machine coin-in minus carded coin-in. They are not revenue.")

    try:
        chosen, suppressed_dim = metrics.monthly_segment_sums(segments, months, dimension)
    except ValueError as exc:
        st.error(str(exc), title="Segment rows are missing")
        st.stop()
    if not suppressed_dim.empty:
        st.warning(
            f"{len(suppressed_dim)} cells in this attribute are suppressed and left out.",
            title="Suppressed cells",
        )
    chosen_total = float(chosen["coin_in_usd"].sum())
    if abs(chosen_total - selected_wagers) > 1:
        st.error(
            f"This attribute's wagers differ from the carded total by {money(chosen_total - selected_wagers)}.",
            title="Segment wagers do not reconcile",
        )
    st.plotly_chart(
        charts.value_bars(chosen, "segment", "coin_in_usd", f"Carded wagers by {dimension_label or dimension}", "USD"),
        width="stretch",
        theme="streamlit",
        alt="Carded wagers by the selected guest attribute for the selected months",
    )
    st.caption("One attribute at a time. These bars are wagering volume for the selected months, not visits and not casino revenue.")

st.subheader("Where the carded wagers sit")
st.caption(str(top5["scope"]))
wager_shares = evidence.loc[
    (evidence["question"] == "Q4")
    & (evidence["metric"] == "share of coin_in_usd")
    & evidence["subgroup"].astype(str).str.contains("wagerers")
].copy()
wager_shares["label"] = wager_shares["subgroup"].astype(str).str.replace(" top wagerers (by coin-in)", "", regex=False)
wager_shares["share_percent"] = wager_shares["estimate"] * 100
with st.grid(3, gap="medium", min_column_width=180):
    st.metric(
        "Top 5% of wagerers",
        percent(top5["estimate"]),
        border=True,
        height="stretch",
        help=(
            f"Share of carded wagers, not of losses. {top5['comparison']}. "
            f"Interval {percent(top5['ci_low'])} to {percent(top5['ci_high'])}."
        ),
    )
    st.metric(
        "Locals' share of wagers",
        percent(metrics.ratio(local_wagers, carded_wagers)),
        border=True,
        height="stretch",
        help="Local home region divided by all carded wagers, six-month window. Not a share of comps and not a share of visits.",
    )
    st.metric(
        "Gini of carded wagers",
        f"{float(gini['estimate']):.2f}",
        border=True,
        height="stretch",
        help=f"0 is equal, 1 is fully concentrated. Interval {float(gini['ci_low']):.2f} to {float(gini['ci_high']):.2f}.",
    )
st.plotly_chart(
    charts.value_bars(wager_shares, "label", "share_percent", "Share of carded wagers", "Percent"),
    width="stretch",
    theme="streamlit",
    alt="Share of carded wagers held by the top 1, 5, and 10 percent of players ranked by wagering",
)
st.caption(
    "Players are ranked by carded wagers. A ranking by losses is a different descriptive cut, and this page does not use it "
    "to single anyone out. The month filter does not change these shares."
)

with st.expander("How carded play is measured"):
    st.markdown(
        "Carded wagers, payouts, and the net player outcome come from `gold_player_segments`, dimension `all`. "
        "The six-month row counts distinct players. Monthly money totals add up to that row; monthly player counts do not. "
        f"House win on this carded play is {money(float(window_row['house_win_usd']))}, the opposite of the net player outcome. "
        f"Theoretical win on the same play is {money(float(window_row['theo_usd']))}. It is not added to house win. "
        "Machine coin-in in the share includes untracked play. Segments are never crossed and never joined to another table."
    )

st.subheader("Comps")
st.caption("Question 5 · F&B comp cost, already inside cost of goods. Menu value is not cost.")

if not months:
    st.info("Select at least one month to see comp cost for those months.", title="No months selected")
    comp_selected = None
    face_selected = None
else:
    property_months = property_daily.loc[property_daily["gaming_date"].dt.strftime("%Y-%m").isin(months)]
    comp_selected = float(property_months["comp_cost_usd"].sum())
    face_selected = float(property_months["comp_face_value_usd"].sum())
    cogs_selected = float(property_months["pos_cogs_usd"].sum())
    with st.grid(3, gap="medium", min_column_width=180):
        st.metric(
            "Comp cost",
            money(comp_selected, 2),
            border=True,
            height="stretch",
            help="Economic cost of F&B comps. Already included in F&B cost of goods. Not subtracted again.",
        )
        st.metric(
            "Menu value",
            money(face_selected, 2),
            border=True,
            height="stretch",
            help="Face value of the same comps. Not cost, and not added to cost or cash.",
        )
        st.metric(
            "Share of F&B cost of goods",
            percent(metrics.ratio(comp_selected, cogs_selected)),
            border=True,
            height="stretch",
            help="Comp cost divided by total F&B cost of goods. The remainder is non-comp cost of goods.",
        )
    daily = property_months.sort_values("gaming_date")
    comp_chart = charts.money_lines(
        daily,
        "Comp cost and menu value",
        [
            ("comp_cost_usd", "Comp cost", charts.GOLD),
            ("comp_face_value_usd", "Menu value", charts.SLATE),
        ],
    )
    comp_chart.add_vline(x="2025-05-01", line_dash="dot", line_color=charts.IVORY, line_width=1)
    comp_chart.add_annotation(x="2025-05-01", y=1, yref="paper", text="May 1", showarrow=False, yshift=12)
    st.plotly_chart(comp_chart, width="stretch", theme="streamlit", alt="Daily F&B comp cost and menu value, with May 1 marked")
    st.caption("The line marks the points-price change. Cost and menu value stay separate. The full-window comp cost is $190,669.30.")

    comp_by_segment, suppressed_comp = metrics.monthly_segment_sums(segments, months, dimension)
    if not suppressed_comp.empty:
        st.warning(
            f"{len(suppressed_comp)} comp cells in this attribute are suppressed and left out.",
            title="Suppressed cells",
        )
    st.plotly_chart(
        charts.value_bars(
            comp_by_segment,
            "segment",
            "comp_cost_usd",
            f"Comp cost by {dimension_label or dimension}",
            "USD",
        ),
        width="stretch",
        theme="streamlit",
        alt="F&B comp cost by the selected guest attribute for the selected months",
    )
    st.caption("Comp cost for the same single attribute as the wagering chart. Not a recommendation to shift comps toward any group.")

st.subheader("The May 1 points price")
st.caption("Association only. Other changes in May and June are not ruled out.")
policy = evidence.loc[
    (evidence["question"] == "Q5")
    & (evidence["topic"] == "may 1 comp price change")
    & (evidence["subgroup"] == "primary: feb-jun its, weekday + trend")
    & evidence["metric"].isin([metric for metric, _label in POLICY_METRICS])
].copy()
policy["label"] = policy["metric"].map(dict(POLICY_METRICS))
st.plotly_chart(
    charts.effect_bars(policy, "May–June change, adjusted for weekday and trend", "label"),
    width="stretch",
    theme="streamlit",
    alt="Trend-adjusted relative change after the May 1 points-price change, with intervals",
)
points_its = policy.loc[policy["metric"] == "points redeemed per day"].iloc[0]
st.markdown(
    f"Points redeemed per day fell **{signed_percent(points_raw['estimate'])}** on the February–April versus May–June comparison, "
    f"and **{signed_percent(points_its['estimate'])}** after a weekday and trend adjustment. "
    "Carded wagers per day do not clear zero on that adjusted comparison. "
    "This is a before/after association, not a proven effect of the price change."
)
with st.grid(2, gap="medium", min_column_width=220):
    st.metric(
        "Locals' share of comp cost",
        percent(local_comp["estimate"]),
        border=True,
        height="stretch",
        help=f"Interval {percent(local_comp['ci_low'])} to {percent(local_comp['ci_high'])}. {local_comp['causal_interpretation']}",
    )
    st.metric(
        "Locals' share of carded theo",
        percent(local_theo["estimate"]),
        border=True,
        height="stretch",
        help="Theoretical win, not wagers. Locals already receive a larger share of comp cost than of carded theoretical win.",
    )
st.markdown(
    f"Six-month comp cost is **{money(comp_cost, 2)}**. It is already inside F&B cost of goods of {money(pos_cogs, 2)}, "
    f"leaving {money(non_comp_cogs, 2)} of non-comp cost. "
    "Locals already account for more of the comp cost than of carded theoretical win, and the local label is confounded with longer membership. "
    "That does not support a larger comp budget, and it is not a reason to aim comps at players with large losses."
)

with st.expander("How comps are counted"):
    st.markdown(
        "`non_comp_cogs = pos_cogs − comp_cost`. Comp cost is not subtracted a second time, and menu value is never added to cost or cash. "
        "The May 1 chart uses the interrupted time series that adjusts for weekday and trend. "
        "The raw points comparison is shown in the sentence beside it. Both are associations. "
        "Guest cells with fewer than 20 players would be suppressed; the smallest published cell in this build has 129 players."
    )
