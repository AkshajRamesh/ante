#aggregations that respect gold grains. ratios are ratios of sums.
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import pandas as pd

#rounded dollars from the validated july plan. the app displays gold's own sums
#and reports a mismatch if those sums do not round to these figures.
DECISION_DOLLARS = {
    "spend": 6600,
    "unallocated": 93400,
    "budget": 100000,
    "net_low": 2059,
    "net_central": 13272,
    "net_high": 22724,
}

LIST_KEYS = {
    "original": "original",
    "exposure": "exposure-adjusted",
    "demand": "demand-adjusted",
    "actual": "actual-win-adjusted",
}


def dollars(value) -> int:
    return int(Decimal(str(float(value))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ratio(numerator, denominator):
    denominator = float(denominator)
    if denominator == 0 or pd.isna(denominator):
        return None
    return float(numerator) / denominator


def z_score(residual, variance):
    variance = float(variance)
    if variance <= 0 or pd.isna(variance):
        return None
    return float(residual) / (variance ** 0.5)


def unique_value(frame: pd.DataFrame, column: str) -> float:
    values = frame[column].dropna().astype(float).unique()
    if len(values) != 1:
        raise ValueError(f"{column} is not a single value across rows, so it cannot be shown once.")
    return float(values[0])


def between_dates(frame: pd.DataFrame, start, end) -> pd.DataFrame:
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    dates = frame["gaming_date"]
    return frame.loc[(dates >= start) & (dates <= end)].copy()


def property_kpis(daily: pd.DataFrame) -> dict:
    coin = float(daily["coin_in_usd"].sum())
    house = float(daily["house_win_usd"].sum())
    theo = float(daily["theo_usd"].sum())
    scheduled = float(daily["scheduled_machine_hours"].sum())
    available = float(daily["available_machine_hours"].sum())
    return {
        "days": int(daily["gaming_date"].nunique()),
        "coin_in": coin,
        "house_win": house,
        "theo": theo,
        "hold": ratio(house, coin),
        "availability": ratio(available, scheduled),
        "downtime_hours": float(daily["downtime_hours"].sum()),
        "spark_coin": daily.sort_values("gaming_date")["coin_in_usd"].astype(float).tolist(),
        "spark_house": daily.sort_values("gaming_date")["house_win_usd"].astype(float).tolist(),
    }


def machine_property_gap(machine_daily: pd.DataFrame, property_daily: pd.DataFrame) -> float:
    #both are on-floor gaming-day totals. a gap means the page would double count or drop days.
    machine_coin = float(machine_daily["coin_in_usd"].sum())
    property_coin = float(property_daily["coin_in_usd"].sum())
    return machine_coin - property_coin


def plan_summary(plan: pd.DataFrame) -> dict:
    summary = {
        "budget": unique_value(plan, "budget_usd"),
        "spend": unique_value(plan, "plan_total_cost_usd"),
        "unallocated": unique_value(plan, "unallocated_usd"),
        "action_types": int(plan["action_type_id"].nunique()),
        "targets": int(len(plan)),
        "net_low": float(plan["net_low_usd"].sum()),
        "net_central": float(plan["net_central_usd"].sum()),
        "net_high": float(plan["net_high_usd"].sum()),
        "benefit_central": float(plan["benefit_central_usd"].sum()),
        "scope": str(plan["scope"].iloc[0]),
    }
    summary["rounded"] = {key: dollars(summary[key]) for key in DECISION_DOLLARS}
    return summary


def decision_mismatches(summary: dict) -> list[str]:
    problems = []
    for key, expected in DECISION_DOLLARS.items():
        got = summary["rounded"][key]
        if got != expected:
            problems.append(f"{key} rounds to {got:,}, not the validated {expected:,}.")
    if summary["action_types"] > 3:
        problems.append(f"The plan uses {summary['action_types']} action types; the validated plan uses at most 3.")
    return problems


def build_log_counts(log: pd.DataFrame) -> dict:
    columns = ["checks_pass", "checks_fail", "checks_warn", "checks_info"]
    counts = {}
    for column in columns:
        values = log[column].dropna().astype(int).unique()
        if len(values) != 1:
            raise ValueError(f"{column} differs across gold_build_log rows.")
        counts[column.removeprefix("checks_")] = int(values[0])
    return counts


def list_named(bottom: pd.DataFrame, key: str) -> pd.DataFrame:
    prefix = LIST_KEYS[key]
    names = sorted(name for name in bottom["list_name"].unique() if str(name).startswith(prefix))
    if len(names) != 1:
        raise ValueError(f"Expected one bottom-ten list starting with {prefix!r}, found {names}.")
    return bottom.loc[bottom["list_name"] == names[0]].sort_values("list_rank")


def short_list_name(name: str) -> str:
    for key, prefix in LIST_KEYS.items():
        if str(name).startswith(prefix):
            return {
                "original": "Raw house win",
                "exposure": "Theo per available hour",
                "demand": "Demand index",
                "actual": "Actual-win index",
            }[key]
    return str(name)


def period_machines(daily: pd.DataFrame, zones: list[str] | None, themes: list[str] | None, search: str) -> pd.DataFrame:
    part = daily
    if zones:
        part = part.loc[part["zone_name"].isin(zones)]
    if themes:
        part = part.loc[part["theme"].isin(themes)]
    query = (search or "").strip()
    if query:
        part = part.loc[part["machine_id"].str.contains(query, case=False, na=False)]
    if part.empty:
        return part
    grouped = part.groupby("machine_id", as_index=False).agg(
        coin_in_usd=("coin_in_usd", "sum"),
        house_win_usd=("house_win_usd", "sum"),
        theo_usd=("theo_usd", "sum"),
        available_hours=("available_hours", "sum"),
        scheduled_hours=("scheduled_hours", "sum"),
        luck_residual_usd=("luck_residual_usd", "sum"),
        luck_variance_usd2=("luck_variance_usd2", "sum"),
        hit_spins=("hit_spins", "sum"),
        expected_hit_spins=("expected_hit_spins", "sum"),
        hit_variance=("hit_variance", "sum"),
        paid_spins=("paid_spins", "sum"),
        days=("gaming_date", "nunique"),
    )
    zone_hours = (
        part.groupby(["machine_id", "zone_name"], as_index=False)["available_hours"].sum()
        .sort_values("available_hours")
        .groupby("machine_id", as_index=False)
        .tail(1)[["machine_id", "zone_name"]]
    )
    theme_hours = (
        part.groupby(["machine_id", "theme"], as_index=False)["available_hours"].sum()
        .sort_values("available_hours")
        .groupby("machine_id", as_index=False)
        .tail(1)[["machine_id", "theme"]]
    )
    grouped = grouped.merge(zone_hours, on="machine_id", how="left").merge(theme_hours, on="machine_id", how="left")
    grouped["observed_hold"] = grouped["house_win_usd"] / grouped["coin_in_usd"].where(grouped["coin_in_usd"] != 0)
    grouped["theo_per_available_hour"] = grouped["theo_usd"] / grouped["available_hours"].where(grouped["available_hours"] != 0)
    grouped["luck_z_in_selection"] = [
        z_score(residual, variance)
        for residual, variance in zip(grouped["luck_residual_usd"], grouped["luck_variance_usd2"])
    ]
    grouped["hit_z_in_selection"] = [
        z_score(hits - expected, variance)
        for hits, expected, variance in zip(grouped["hit_spins"], grouped["expected_hit_spins"], grouped["hit_variance"])
    ]
    grouped = grouped.sort_values(["house_win_usd", "machine_id"], ascending=[True, True]).reset_index(drop=True)
    grouped.insert(0, "rank_in_selection", grouped.index + 1)
    return grouped


def one_row(frame: pd.DataFrame, **filters) -> pd.Series:
    part = frame
    for column, value in filters.items():
        part = part.loc[part[column] == value]
    if len(part) != 1:
        raise ValueError(f"Expected one Gold row for {filters}, found {len(part)}.")
    return part.iloc[0]


def zone_rates(zone_daily: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
    #ratio of sums, never the average of daily rates
    totals = zone_daily.groupby(group_columns, as_index=False).agg(
        coin_in_usd=("coin_in_usd", "sum"),
        theo_usd=("theo_usd", "sum"),
        available_hours=("available_hours", "sum"),
        scheduled_machine_hours=("scheduled_machine_hours", "sum"),
        downtime_hours=("downtime_hours", "sum"),
        entries_observed=("entries_observed", "sum"),
        covered_sensor_hours=("covered_sensor_hours", "sum"),
        days=("gaming_date", "nunique"),
    )
    totals["coin_per_available_hour"] = totals["coin_in_usd"] / totals["available_hours"].where(totals["available_hours"] > 0)
    totals["entries_per_covered_hour"] = totals["entries_observed"] / totals["covered_sensor_hours"].where(totals["covered_sensor_hours"] > 0)
    totals["availability"] = totals["available_hours"] / totals["scheduled_machine_hours"].where(totals["scheduled_machine_hours"] > 0)
    return totals


SEGMENT_MONEY = (
    "coin_in_usd",
    "payout_usd",
    "net_usd",
    "house_win_usd",
    "theo_usd",
    "comp_cost_usd",
    "comp_face_value_usd",
    "points_earned",
    "points_redeemed",
    "linked_pos_cash_usd",
)


def monthly_segment_sums(segments: pd.DataFrame, months: list[str], dimension: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    #one dimension at a time. money adds across months; player counts do not, so they are not returned.
    part = segments.loc[
        (segments["period_type"] == "month")
        & (segments["dimension"] == dimension)
        & (segments["period_month"].isin(months))
    ].copy()
    if part.empty:
        raise ValueError(f"No monthly rows for {dimension} in {months}.")
    suppressed = part.loc[part["suppressed"].fillna(False)]
    usable = part.loc[~part["suppressed"].fillna(False)]
    totals = usable.groupby("segment", as_index=False)[list(SEGMENT_MONEY)].sum()
    return totals, suppressed


def daily_totals(daily: pd.DataFrame) -> pd.DataFrame:
    totals = (
        daily.groupby("gaming_date", as_index=False)[["coin_in_usd", "house_win_usd", "theo_usd"]]
        .sum()
        .sort_values("gaming_date")
    )
    return totals
