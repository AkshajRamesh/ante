#selects a small set of verified gold facts for one question.
#numbers come from the snapshot. this module does not call a model.
from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from ante.metrics import decision_mismatches, dollars, list_named, plan_summary, property_kpis
from ante.snapshot import FORBIDDEN_COLUMNS, SnapshotGold

SOURCES = {
    "Q1": "Machine Performance · Q1",
    "Q2": "Demand & Location · Q2",
    "Q3": "Downtime & Operations · Q3",
    "Q4": "Players & Loyalty · Q4",
    "Q5": "Players & Loyalty · Q5",
    "Q6": "Investment Decision · Q6",
    "forecast": "Model Validation · forecast",
}

_INTENTS = (
    ("bottom", ("bottom ten", "bottom 10", "replace the", "replacement", "house win rank", "demand-adjusted", "demand rank")),
    ("inspect", ("m-1158", "1158", "inspect", "inspection", "paytable", "payout pattern", "hit rate", "malfunction")),
    ("invest", ("13,272", "13272", "93,400", "93400", "6,600", "6600", "unspent", "remaining", "budget", "invest", "net theo", "net benefit", "confident", "uncertain", "not spending", "aren't we spending", "leave", "low case", "high case", "central case")),
    ("forecast", ("ridge", "weekday", "forecast", "baseline", "holdout", "mae", "prediction interval")),
    ("loyalty", ("may 1", "may 1st", "loyalty", "comp price", "points", "policy change")),
    ("closure", ("back forty", "walkway", "closure", "relocat", "moving machines", "move machines")),
    ("downtime", ("downtime", "availability", "maintenance", "fault", "out of service")),
    ("players", ("carded", "gini", "wager", "concentration", "comps", "loyalty player")),
)

_BLOCKED = re.compile(
    r"(target|exploit|single out|identify).{0,60}(player|gambler|guest)s?.{0,40}(loss|loser|lost)"
    r"|(player|gambler|guest)s?.{0,40}(who lost|biggest loss|personal loss)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Fact:
    fact_id: str
    source: str
    kind: str
    text: str


@dataclass(frozen=True)
class Packet:
    facts: tuple[Fact, ...]
    insufficient: bool
    note: str
    build_id: str
    blocked: bool = False

    def ids(self) -> set[str]:
        return {fact.fact_id for fact in self.facts}

    def render(self) -> str:
        lines = [
            f"snapshot {self.build_id}",
            f"insufficient: {str(self.insufficient).lower()}",
            f"note: {self.note}",
        ]
        for fact in self.facts:
            lines.append(f"[{fact.fact_id}] {fact.source} | {fact.kind} | {fact.text}")
        return "\n".join(lines)


def blocks_personal_targeting(question: str) -> bool:
    return _BLOCKED.search(question or "") is not None


OUT_OF_SCOPE = (
    "Sorry, I can only answer questions about ante's dashboard and The Gilded Ace's casino analysis."
)
NO_EVIDENCE = (
    "I don't have enough evidence in the dashboard to answer that reliably. "
    "Try asking about machine performance, demand, downtime, loyalty, investments, or forecasting."
)

# A question the Gold topics do not cover. "Hotel Corridor" is a real zone and does not match these.
_MISSING_TOPIC = re.compile(
    r"hotel booking|hotel bookings|room revenue|hotel revenue|restaurant|"
    r"food and beverage|\bf&b\b|\bspa\b|parking revenue|sportsbook|"
    r"\bpoker\b|table games?|concert revenue|occupancy",
    re.IGNORECASE,
)

# Off-topic only when the question is not also about a Gold topic.
_OFF_TOPIC = re.compile(
    r"(?:"
    r"\d+(?:\.\d+)?\s*(?:[+\-*/x×]|plus|minus|times|divided(?:\s+by)?)\s*\d+"
    r"|world cup|super bowl|olympics?"
    r"|\bweather\b"
    r"|\bpoem\b|\bhaiku\b|\blyrics\b|\bjoke\b|\brecipe\b"
    r"|write (?:me )?(?:a )?(?:poem|story|song|essay)"
    r"|\bcapital of\b|\bpresident of\b"
    r")",
    re.IGNORECASE,
)


def _specific_gold(text: str) -> bool:
    return any(score > 0 for _, score in _scores(text or ""))


def _is_follow_up(question: str, prior: str) -> bool:
    # Short references stay attached to the previous dashboard question.
    # A new off-topic question is not a follow-up, even if one came before it.
    if not prior.strip():
        return False
    if (_OFF_TOPIC.search(question) or _MISSING_TOPIC.search(question)) and not _specific_gold(question):
        return False
    words = re.findall(r"[A-Za-z0-9']+", question)
    if len(words) <= 8:
        return True
    return re.search(r"\b(that|this|those|low case|high case|central case)\b", question, re.IGNORECASE) is not None


def refusal(question: str, prior: str = "") -> str | None:
    # None means the question may be sent to Gemini with a Gold packet.
    # A string is the whole reply, and the model is not called.
    text = " ".join((question or "").split())
    if not text or blocks_personal_targeting(text):
        return None
    if _MISSING_TOPIC.search(text) and not _specific_gold(text):
        return NO_EVIDENCE
    follow = _is_follow_up(text, prior or "")
    topical = _specific_gold(text) or (follow and _specific_gold(prior or ""))
    if _OFF_TOPIC.search(text) and not topical:
        return OUT_OF_SCOPE
    if not topical:
        return NO_EVIDENCE
    return None


def retrieve(source: SnapshotGold, question: str, prior: str = "") -> Packet:
    build_id = str(source.provenance()["build_id"])
    if blocks_personal_targeting(question):
        return Packet((), True, "This question asks for an individual-guest strategy. ante will not send it.", build_id, blocked=True)
    scored = _scores(f"{question}\n{prior}")
    chosen = [name for name, score in scored if score > 0][:2]
    if not chosen:
        return Packet(
            (),
            True,
            "No Gold topic matched. Topics on hand are machine rankings, M-1158, the Back Forty closure, "
            "downtime, carded play, the May 1 comp change, the July plan, and the May–June forecast.",
            build_id,
        )
    facts: list[Fact] = []
    for name in chosen:
        facts.extend(_BUILDERS[name](source))
    facts = _unique(facts)[:16]
    facts = [Fact(fact.fact_id, fact.source, fact.kind, fact.text.replace("$-", "-$")) for fact in facts]
    _assert_public(facts)
    return Packet(tuple(facts), False, "Use only these facts.", build_id)


def _scores(text: str) -> list[tuple[str, int]]:
    hay = text.lower()
    scored = [(name, sum(phrase in hay for phrase in phrases)) for name, phrases in _INTENTS]
    return sorted(scored, key=lambda item: item[1], reverse=True)


def _unique(facts: list[Fact]) -> list[Fact]:
    seen = set()
    kept = []
    for fact in facts:
        if fact.fact_id in seen:
            continue
        seen.add(fact.fact_id)
        kept.append(fact)
    return kept


def _assert_public(facts: list[Fact]) -> None:
    blob = "\n".join(fact.text.lower() for fact in facts)
    for column in FORBIDDEN_COLUMNS:
        if column in blob:
            raise RuntimeError("Evidence selection included a forbidden identifier column.")
    if "gemini_api_key" in blob or "aiza" in blob:
        raise RuntimeError("Evidence selection included a secret.")


def _text(value, fallback: str = "n/a") -> str:
    if value is None or pd.isna(value):
        return fallback
    return " ".join(str(value).split())


def _money(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    amount = dollars(value)
    if amount < 0:
        return f"-${abs(amount):,}"
    return f"${amount:,}"


def _signed_pct(value) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):+.1%}"


def _num(value, digits: int) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.{digits}f}"


def _interval(low, high, formatter) -> str:
    if pd.isna(low) or pd.isna(high):
        return ""
    return f", published interval {formatter(low)} to {formatter(high)}"


def _evidence_fact(row, kind: str) -> Fact:
    estimate = row["estimate"]
    unit = _text(row["unit"], "")
    metric = str(row["metric"])
    shown = _show_estimate(metric, unit, estimate)
    interval = _interval(row["ci_low"], row["ci_high"], lambda value: _show_estimate(metric, unit, value))
    note = _text(row["notes"], "")
    note_bit = f" Note: {note}" if note else ""
    where = "; ".join(part for part in (_text(row["subgroup"], ""), _text(row["comparison"], "")) if part)
    text = (
        f"{where}; {metric} = {shown}{interval}. "
        f"Class: {row['evidence_class']}. Reading: {row['causal_interpretation']}.{note_bit}"
    )
    return Fact(f"E{int(row['evidence_id'])}", SOURCES[str(row["question"])], kind, " ".join(text.split()))


def _show_estimate(metric: str, unit: str, value) -> str:
    if value is None or pd.isna(value):
        return "—"
    name = metric.lower()
    unit_name = unit.lower()
    if "usd" in unit_name or name.endswith("(usd)") or "usd" in name:
        return _money(value)
    if unit_name == "z" or name.endswith(" z") or " z (" in name or name.startswith("luck z") or name.startswith("hit-rate z") or name.startswith("hit z"):
        return _num(value, 2)
    if "probability" in name or "p-value" in name or "p_value" in name:
        return _num(value, 4)
    if "hit rate" in name:
        return _num(value, 4)
    if "fraction" in unit_name or "relative" in unit_name or "hold" in name:
        return _signed_pct(value) if "relative" in unit_name else f"{float(value):.2%}" if "hold" in name else f"{float(value):.1%}"
    return _num(value, 4)


def _rows(frame: pd.DataFrame, limit: int) -> list[Fact]:
    facts = []
    for _, row in frame.head(limit).iterrows():
        kind = "observed" if str(row["evidence_class"]).startswith("observed") else "statistical estimate"
        facts.append(_evidence_fact(row, kind))
    return facts


def _bottom(source: SnapshotGold) -> list[Fact]:
    bottom = source.frame("gold_bottom_ten")
    score = source.frame("gold_machine_scorecard")
    original = list_named(bottom, "original")
    demand = set(list_named(bottom, "demand")["machine_id"])
    exposure = set(list_named(bottom, "exposure")["machine_id"])
    actual = set(list_named(bottom, "actual")["machine_id"])
    original_ids = list(original["machine_id"])
    still_demand = len(set(original_ids) & demand)
    ranks = score.loc[score["machine_id"].isin(original_ids), ["machine_id", "rank_demand_index", "primary_zone"]]
    ranks = ranks.sort_values("rank_demand_index")
    closest = ", ".join(
        f"{row.machine_id} demand rank {int(row.rank_demand_index)} ({row.primary_zone})"
        for row in ranks.itertuples(index=False)
    )
    return [
        Fact(
            "B1",
            SOURCES["Q1"],
            "observed",
            f"Of the 10 machines with the lowest raw house win, {still_demand} are also in the demand-index bottom ten. "
            f"{len(set(original_ids) & exposure)} of 10 remain among the ten weakest on theoretical win per available hour. "
            f"{len(set(original_ids) & actual)} of 10 remain among the ten weakest actual-win indexes. "
            "Demand rank 1 is the weakest demand. Higher rank means stronger demand. "
            f"Original ten, weakest demand first: {closest}.",
        )
    ]


def _inspect(source: SnapshotGold) -> list[Fact]:
    evidence = source.frame("gold_evidence")
    pattern = evidence.loc[evidence["topic"] == "M-1158 payout pattern"].sort_values("evidence_id")
    facts = _rows(pattern, 8)
    plan = source.frame("gold_investment_plan")
    inspection = plan.loc[plan["target"] == "M-1158"]
    if not inspection.empty:
        row = inspection.iloc[0]
        facts.append(
            Fact(
                "P1158",
                SOURCES["Q6"],
                "July scenario",
                f"The July plan funds one inspection of M-1158 at a target cost of {_money(row['target_cost_usd'])}. "
                f"Funding basis: {row['funding_basis']}. Benefit basis: {row['basis']}. "
                f"Central net is {_money(row['net_central_usd'])} because the priced benefit is {_money(row['benefit_central_usd'])}. "
                f"Notes: {row['notes']}",
            )
        )
    return facts


def _invest(source: SnapshotGold) -> list[Fact]:
    plan = source.frame("gold_investment_plan")
    summary = plan_summary(plan)
    problems = decision_mismatches(summary)
    if problems:
        return [Fact("P0", SOURCES["Q6"], "July scenario", "The plan failed its published dollar check, so the totals are withheld. " + " ".join(problems))]
    facts = [
        Fact(
            "P0",
            SOURCES["Q6"],
            "July scenario",
            f"Budget {_money(summary['budget'])}. Plan spend {_money(summary['spend'])}, taken once, not summed from the repeated budget column. "
            f"Left unspent {_money(summary['unallocated'])}. "
            f"Sum of the four action nets: low {_money(summary['net_low'])}, central {_money(summary['net_central'])}, high {_money(summary['net_high'])}. "
            "These are theoretical-win scenarios for July, not money already earned. "
            f"Scope: {summary['scope']}.",
        )
    ]
    for _, row in plan.iterrows():
        facts.append(
            Fact(
                f"P{row['target']}",
                SOURCES["Q6"],
                "July scenario",
                f"{row['action']} {row['target']}. From {_text(row['from_location'])} to {_text(row['to_zone'])}. "
                f"Target cost {_money(row['target_cost_usd'])}. "
                f"Net low/central/high {_money(row['net_low_usd'])} / {_money(row['net_central_usd'])} / {_money(row['net_high_usd'])}. "
                f"Basis: {_text(row['basis'], '')}. {_text(row['central_assumptions'], '')[:180]}",
            )
        )
    scenarios = source.frame("gold_scenarios")
    decisions = scenarios.loc[(scenarios["question"] == "Q6") & (scenarios["kind"].astype(str).str.contains("decision scenario|infeasible"))]
    for _, row in decisions.iterrows():
        facts.append(
            Fact(
                f"S{int(row['scenario_id'])}",
                SOURCES["Q6"],
                "July scenario",
                f"{row['item']}: {row['scenario']}. Kind: {row['kind']}. "
                f"Cost {_money(row['cost_usd']) if pd.notna(row['cost_usd']) else '—'}. "
                f"Central value {_money(row['value_central_usd']) if pd.notna(row['value_central_usd']) else '—'}. "
                f"In the recommended plan: {bool(row['in_recommended_plan'])}. Feasible: {row['feasible']}.",
            )
        )
    return facts


def _forecast(source: SnapshotGold) -> list[Fact]:
    validation = source.frame("gold_model_validation")
    wanted = validation.loc[
        (
            (validation["section"].str.startswith("holdout test") & validation["metric"].eq("mae"))
            | validation["section"].str.startswith("paired holdout")
            | validation["metric"].eq("share of days inside the nominal 90% interval")
            | (validation["section"].eq("disclosure") & validation["metric"].eq("lowest holdout mae (usd)"))
        )
    ]
    facts = []
    for index, row in wanted.iterrows():
        value = row["value"]
        metric = str(row["metric"])
        if "share" in metric:
            shown = f"{float(value):.1%}"
            interval = ""
        elif "mae" in metric or "usd" in metric:
            shown = _money(value)
            interval = _interval(row["ci_low"], row["ci_high"], _money)
        else:
            shown = _num(value, 2)
            interval = _interval(row["ci_low"], row["ci_high"], lambda item: _num(item, 2))
        note = str(row["note"] or "").strip()
        facts.append(
            Fact(
                f"V{index}",
                SOURCES["forecast"],
                "forecast",
                f"{row['section']}; model {row['model']}; {metric} = {shown}{interval}. "
                f"Days: {row['days']}. {note}",
            )
        )
    lead = _forecast_lead(validation)
    if lead is not None:
        facts.insert(0, lead)
    return facts


def _forecast_lead(validation: pd.DataFrame) -> Fact | None:
    holdout = validation.loc[validation["section"].str.startswith("holdout test") & validation["metric"].eq("mae")]
    weekday = holdout.loc[holdout["model"].astype(str).str.startswith("weekday")]
    ridge = holdout.loc[holdout["model"].astype(str).str.startswith("ridge")]
    paired = validation.loc[
        validation["section"].str.startswith("paired holdout")
        & validation["model"].astype(str).str.contains("ridge")
    ]
    if weekday.empty or ridge.empty or paired.empty:
        return None
    gap = paired.iloc[0]
    return Fact(
        "V0",
        SOURCES["forecast"],
        "forecast",
        "Lower MAE is the more accurate forecast. "
        f"Weekday mean holdout MAE {_money(weekday.iloc[0]['value'])} is lower than ridge {_money(ridge.iloc[0]['value'])}. "
        "Ridge did not outperform the weekday baseline. "
        f"Ridge minus weekday is {_money(gap['value'])}"
        f"{_interval(gap['ci_low'], gap['ci_high'], _money)}. "
        "That interval includes zero, so the holdout does not prove the gap is a real difference. "
        "Do not say that the weekday model failed to beat ridge.",
    )


def _loyalty(source: SnapshotGold) -> list[Fact]:
    evidence = source.frame("gold_evidence")
    may = evidence.loc[
        (evidence["topic"] == "may 1 comp price change")
        & evidence["subgroup"].astype(str).str.startswith("primary: feb-apr")
        & evidence["metric"].isin(["carded coin-in per day", "carded theo per day", "comp cost per day", "points redeemed per day"])
    ]
    return _rows(may.sort_values(["metric", "evidence_id"]), 8)


def _closure(source: SnapshotGold) -> list[Fact]:
    evidence = source.frame("gold_evidence")
    headline = evidence.loc[
        (evidence["topic"] == "back forty walkway closure")
        & (evidence["subgroup"] == "all machines in zone")
        & (evidence["comparison"] == "all other zones | closure vs pre")
        & evidence["metric"].isin(["coin_in_per_avail_hour", "entries_per_covered_hour"])
    ]
    moves = evidence.loc[
        (evidence["topic"] == "machine relocations")
        & evidence["metric"].eq("coin-in per hour vs peers")
    ]
    facts = _rows(headline, 4) + _rows(moves, 4)
    plan = source.frame("gold_investment_plan")
    funded = plan.loc[plan["from_location"].notna()]
    for _, row in funded.iterrows():
        facts.append(
            Fact(
                f"P{row['target']}",
                SOURCES["Q6"],
                "July scenario",
                f"Funded move {row['target']} from {row['detail']}. "
                f"Central net {_money(row['net_central_usd'])}. This is a July scenario, not an observed result of the walkway closure. "
                f"Assumptions: {row['central_assumptions']}",
            )
        )
    return facts


def _downtime(source: SnapshotGold) -> list[Fact]:
    daily = source.frame("gold_property_daily")
    kpis = property_kpis(daily)
    facts = [
        Fact(
            "D1",
            SOURCES["Q3"],
            "observed",
            f"Over {kpis['days']} gaming days, availability is {kpis['availability']:.2%} "
            f"(available machine hours divided by scheduled machine hours) and downtime is {kpis['downtime_hours']:,.0f} hours.",
        )
    ]
    evidence = source.frame("gold_evidence")
    lost = evidence.loc[evidence["topic"].isin(["lost-play model", "technician waiting time"])]
    facts.extend(_rows(lost, 6))
    return facts


def _players(source: SnapshotGold) -> list[Fact]:
    evidence = source.frame("gold_evidence")
    play = evidence.loc[evidence["topic"] == "concentration of carded play"]
    keep = play.loc[play["metric"].astype(str).str.contains("gini|top 5|share|carded", case=False)]
    if keep.empty:
        keep = play
    return _rows(keep.sort_values("evidence_id"), 6)


_BUILDERS = {
    "bottom": _bottom,
    "inspect": _inspect,
    "invest": _invest,
    "forecast": _forecast,
    "loyalty": _loyalty,
    "closure": _closure,
    "downtime": _downtime,
    "players": _players,
}
