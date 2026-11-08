#one grounded gemini call. the model cannot run code, sql, or read the filesystem.
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from google import genai
from google.genai import errors, types

from ante.evidence import Fact, Packet

#verified against this account with a live generateContent call.
#list() is not permitted for the key, so a 404 walks this cost-conscious order.
PREFERENCE = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-flash-latest",
)

SESSION_LIMIT = 30
_resolved_model: str | None = None

_SYSTEM = """You are ante, the floor analyst for The Gilded Ace, a fictional casino.
Answer only from the evidence packet. Prior conversation is context for the wording of the question, not a source of numbers.

Rules:
- Keep the three classes separate. Observed means a January–June result that already happened. A statistical estimate is a modeled historical effect or association. A July scenario, including the low, central, and high theoretical-win cases, is a projection, not money already earned and not a guarantee.
- A forecast is not an observed result.
- Do not call a July scenario or a forecast observed, realized, earned, or guaranteed.
- Do not turn an association, a forecast, or a scenario into money already earned or into a proven cause.
- Do not invent numbers, machines, dates, or citations.
- Every number in the answer must appear in a fact you cite.
- Put fact ids only in the citations array.
- If the packet says the evidence is insufficient, say that the available evidence does not answer the question. Do not guess.
- Do not propose targeting an individual guest from losses.
- Do not write SQL, Python, or file commands.
- Never open with "As a floor analyst", "As the floor analyst", "As floor analyst", or "I am ante".
- Copy every figure exactly as written in the cited fact, including decimals and signs.

Response shape for an ordinary question, about 40 to 75 words:
- Line 1 is one short conclusion. Bold that whole line with ** **.
- Leave one blank line.
- Then two or three Markdown bullets, each starting with "- ". Bold the important number in the bullet.
- No greeting, no closing paragraph, and no sentence after the last bullet.
- Say whether a figure is observed, an estimate, a forecast, or a July scenario inside a bullet.
- Use more than 75 words only when dropping a qualification would hide real uncertainty. Still use the conclusion and the bullets. Do not add a closing paragraph.
- The answer field is Markdown with real line breaks. Do not put the bullets in one paragraph.
"""


class AnalystError(Exception):
    #message is safe to show. it must not contain the key.
    pass


@dataclass(frozen=True)
class Reply:
    text: str
    citations: tuple[Fact, ...]
    model: str
    insufficient: bool
    stray_numbers: tuple[str, ...]


def ask(*, api_key: str, question: str, packet: Packet, history: list[dict], viewing: str = "") -> Reply:
    if not api_key.strip():
        raise AnalystError("Gemini is not configured. The other pages still work.")
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=30_000,
            retry_options=types.HttpRetryOptions(attempts=2, http_status_codes=[429, 500, 502, 503, 504]),
        ),
    )
    prompt = _prompt(question, packet, history, viewing)
    try:
        model, response = _generate(client, prompt, api_key)
    except AnalystError:
        raise
    except Exception as exc:
        raise AnalystError(_public_error(exc, api_key)) from None
    try:
        text = (response.text or "").strip()
    except Exception:
        raise AnalystError("Gemini returned an unusable answer. Nothing was invented in its place.") from None
    if not text:
        raise AnalystError("Gemini returned an empty answer. Nothing was invented in its place.")
    if api_key in text:
        raise AnalystError("The answer was discarded because it echoed a credential.")
    parsed = _parse(text)
    if parsed is None:
        raise AnalystError("Gemini returned an unusable answer. Nothing was invented in its place.")
    allowed = {fact.fact_id: fact for fact in packet.facts}
    cited = []
    for fact_id in parsed["citations"]:
        fact = allowed.get(str(fact_id).strip())
        if fact is not None and fact not in cited:
            cited.append(fact)
    prose = _shape_markdown(_strip_lead(_strip_unknown_ids(parsed["answer"], set(allowed))))
    prose = _guard_classes(question, prose, packet.facts)
    stray = _stray_numbers(prose, packet.render())
    return Reply(prose, tuple(cited), model, bool(parsed["insufficient"] or packet.insufficient), stray)


def _generate(client, prompt: str, api_key: str):
    global _resolved_model
    order = ([_resolved_model] if _resolved_model else []) + [name for name in PREFERENCE if name != _resolved_model]
    last_error = None
    for model in order:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM,
                    temperature=0.2,
                    max_output_tokens=800,
                    response_mime_type="application/json",
                    # This model rejects a zero thinking budget. Minimal keeps the JSON
                    # from being cut off by a long hidden thought.
                    thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
                ),
            )
        except errors.ClientError as exc:
            last_error = exc
            if getattr(exc, "code", None) == 404:
                continue
            raise AnalystError(_public_error(exc, api_key)) from None
        except errors.APIError as exc:
            raise AnalystError(_public_error(exc, api_key)) from None
        _resolved_model = model
        return model, response
    raise AnalystError(_public_error(last_error or RuntimeError("No Gemini model responded."), api_key))


def _prompt(question: str, packet: Packet, history: list[dict], viewing: str) -> str:
    prior = []
    for turn in history[-4:]:
        role = "Judge" if turn.get("role") == "user" else "ante"
        text = " ".join(str(turn.get("text", "")).split())[:400]
        if text:
            prior.append(f"{role}: {text}")
    prior_block = "\n".join(prior) if prior else "None."
    page = viewing.strip() or "the dashboard"
    return (
        f"Question:\n{question.strip()[:500]}\n\n"
        f"The judge is viewing the {page} page. That is only the page name. "
        "Do not invent which filters are selected, and do not treat it as a source of numbers.\n\n"
        f"Prior conversation, not evidence:\n{prior_block}\n\n"
        f"Evidence packet:\n{packet.render()}"
    )


def _strip_lead(text: str) -> str:
    cleaned = text.strip()
    leads = (
        "as a floor analyst for the gilded ace,",
        "as the floor analyst for the gilded ace,",
        "as floor analyst for the gilded ace,",
        "as a floor analyst,",
        "as the floor analyst,",
        "as floor analyst,",
    )
    lower = cleaned.lower()
    for lead in leads:
        if lower.startswith(lead):
            cleaned = cleaned[len(lead):].strip()
            break
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _parse(raw: str) -> dict | None:
    # Only a JSON object with a written answer is shown. Raw model text is not.
    body = raw.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?", "", body).strip()
        body = re.sub(r"```$", "", body).strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    answer = _answer_text(parsed)
    if not answer:
        return None
    citations = parsed.get("citations") or []
    if not isinstance(citations, list):
        citations = []
    return {
        "answer": answer,
        "citations": [str(item) for item in citations],
        "insufficient": bool(parsed.get("insufficient")),
    }


def _answer_text(parsed: dict) -> str:
    answer = parsed.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    conclusion = parsed.get("conclusion") or parsed.get("headline")
    bullets = parsed.get("bullets") if isinstance(parsed.get("bullets"), list) else parsed.get("points")
    if not isinstance(conclusion, str) or not conclusion.strip():
        return ""
    lines = [conclusion.strip()]
    if isinstance(bullets, list):
        written = []
        for item in bullets:
            text = str(item).strip()
            if not text:
                continue
            if not text.startswith(("- ", "* ")):
                text = "- " + text
            written.append(text)
        if written:
            lines.append("")
            lines.extend(written)
    return "\n".join(lines).strip()


def _strip_unknown_ids(text: str, allowed: set[str]) -> str:
    def replace(match: re.Match) -> str:
        token = match.group(1)
        if token in allowed:
            return ""
        return "" if re.fullmatch(r"E\d+|P[\w\-]+|S\d+|V\d+|B\d+|D\d+", token) else token

    cleaned = re.sub(r"\[?(E\d+|PM-\d+|P\d+|S\d+|V\d+|B\d+|D\d+)\]?", replace, text)
    tidied = []
    for line in cleaned.splitlines():
        # Drop the commas left behind when an in-text fact id is removed.
        line = re.sub(r"(?<!\d)\s*,(?:\s*,)+", "", line)
        line = re.sub(r"\s*,\s*(?=[.,;:])", "", line)
        line = re.sub(r"\s+([.,;:])", r"\1", line)
        tidied.append(" ".join(line.split()))
    return "\n".join(tidied).strip()


def _shape_markdown(text: str) -> str:
    #keep the model's words. only restore the blank line a markdown list needs.
    lines = [line for line in text.splitlines()]
    shaped = []
    for line in lines:
        bullet = line.lstrip().startswith(("- ", "* "))
        if bullet and shaped and shaped[-1] != "":
            shaped.append("")
        shaped.append(line)
    return "\n".join(shaped).strip()


_SCENARIO_KINDS = {"July scenario", "forecast"}
_MONEY = re.compile(r"\$\d[\d,]*(?:\.\d+)?")
_CLAIM = re.compile(
    r"\b(?:actually earned|already earned|observed|realized|realised|guaranteed|earned)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(?:not|no|never|without|rather than|instead of)\b|n['’]t\s*$",
    re.IGNORECASE,
)
_DENIES_REALIZED = re.compile(
    r"not (?:an )?observed|no observed|not money already earned|not guaranteed|"
    r"not realized|not realised|did not earn|have not earned|not actual|"
    r"rather than (?:an )?observed|instead of (?:an )?observed",
    re.IGNORECASE,
)
_ASKS_EARNINGS = re.compile(
    r"\b(?:actually earn(?:ed)?|did\b.+\bearn|post-relocation|realized|realised|already earned)\b",
    re.IGNORECASE,
)
_ASKS_GUARANTEE = re.compile(r"\bguaranteed\b", re.IGNORECASE)


def _norm_amount(token: str) -> str:
    cleaned = token.replace("$", "").replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return cleaned
    if value.is_integer():
        return str(int(value))
    return format(value, "f").rstrip("0").rstrip(".")


def _amounts_by_kind(facts: tuple[Fact, ...]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for fact in facts:
        found = {_norm_amount(token) for token in _MONEY.findall(fact.text)}
        grouped.setdefault(fact.kind, set()).update(found)
    return grouped


def _scenario_only(grouped: dict[str, set[str]]) -> set[str]:
    scenario: set[str] = set()
    for kind, amounts in grouped.items():
        if kind in _SCENARIO_KINDS:
            scenario |= amounts
    return scenario - grouped.get("observed", set())


def _class_label(amount: str, grouped: dict[str, set[str]]) -> str:
    if amount in grouped.get("July scenario", set()):
        return "July scenario"
    if amount in grouped.get("forecast", set()):
        return "forecast"
    return "projection"


def _negated(text: str, start: int) -> bool:
    return _NEGATION.search(text[max(0, start - 55):start]) is not None


def _nearest_scenario(sentence: str, start: int, end: int, scenario: set[str]) -> str | None:
    best = None
    best_distance = 49
    for match in _MONEY.finditer(sentence):
        amount = _norm_amount(match.group(0))
        if amount not in scenario:
            continue
        if match.end() <= start:
            distance = start - match.end()
        elif match.start() >= end:
            distance = match.start() - end
        else:
            distance = 0
        if distance < best_distance:
            best = amount
            best_distance = distance
    return best


def _rewrite_claim(sentence: str, match: re.Match, label: str) -> str:
    word = match.group(0).lower()
    following = sentence[match.end():match.end() + 4]
    if word == "observed" and following.lower().startswith(" as"):
        replacement = f"a {label} figure of"
        return sentence[:match.start()] + replacement + sentence[match.end() + 3:]
    if word == "observed":
        replacement = f"a {label}"
    elif word == "guaranteed":
        replacement = "not guaranteed"
    elif "earned" in word:
        replacement = f"did not earn that money; the {label} value is"
    else:
        replacement = f"is not realized earnings; the {label} value is"
    return sentence[:match.start()] + replacement + sentence[match.end():]


def _fix_sentence(sentence: str, scenario: set[str], grouped: dict[str, set[str]]) -> str:
    replacements = []
    for match in _CLAIM.finditer(sentence):
        if _negated(sentence, match.start()):
            continue
        amount = _nearest_scenario(sentence, match.start(), match.end(), scenario)
        if amount is None:
            continue
        replacements.append((match.start(), match.end(), _class_label(amount, grouped), match))
    if not replacements:
        return sentence
    # Earlier replacements shift later indexes, so apply from the right.
    updated = sentence
    for _, _, label, match in reversed(replacements):
        updated = _rewrite_claim(updated, match, label)
    if _sentence_mislabels(updated, scenario):
        return _fallback_sentence(sentence, scenario, grouped)
    return updated


def _fallback_sentence(sentence: str, scenario: set[str], grouped: dict[str, set[str]]) -> str:
    amounts = sorted(_sentence_amounts(sentence, scenario), key=float)
    shown = ", ".join(f"${int(amount):,}" if amount.isdigit() else f"${amount}" for amount in amounts) or "This figure"
    labels = {_class_label(amount, grouped) for amount in amounts}
    label = "July scenario" if "July scenario" in labels or not labels else next(iter(labels))
    return f"{shown} is a {label} figure, not observed earnings and not a guaranteed result."


def _sentence_amounts(sentence: str, scenario: set[str]) -> set[str]:
    return {amount for amount in (_norm_amount(match.group(0)) for match in _MONEY.finditer(sentence)) if amount in scenario}


def _sentence_mislabels(sentence: str, scenario: set[str]) -> bool:
    for match in _CLAIM.finditer(sentence):
        if _negated(sentence, match.start()):
            continue
        if _nearest_scenario(sentence, match.start(), match.end(), scenario) is not None:
            return True
    return False


def _fix_line(line: str, scenario: set[str], grouped: dict[str, set[str]]) -> str:
    stripped = line.lstrip()
    indent = line[:len(line) - len(stripped)]
    prefix = ""
    body = stripped
    if stripped.startswith("- "):
        prefix = "- "
        body = stripped[2:]
    elif stripped.startswith("* "):
        prefix = "* "
        body = stripped[2:]
    parts = re.split(r"(?<=[.!?])\s+", body)
    fixed = " ".join(_fix_sentence(part, scenario, grouped) for part in parts if part)
    if stripped.startswith("**") and stripped.endswith("**") and not (fixed.startswith("**") and fixed.endswith("**")):
        fixed = f"**{fixed.strip('*').strip()}**"
    if re.match(r"^\s*\**\s*Yes\b", fixed) and "not guaranteed" in fixed.lower():
        fixed = re.sub(r"\bYes\b", "No", fixed, count=1)
    return f"{indent}{prefix}{fixed}"


def _guard_classes(question: str, prose: str, facts: tuple[Fact, ...]) -> str:
    grouped = _amounts_by_kind(facts)
    scenario = _scenario_only(grouped)
    if not scenario:
        return prose
    lines = [_fix_line(line, scenario, grouped) if line.strip() else line for line in prose.splitlines()]
    guarded = "\n".join(lines).strip()
    if not _needs_denial(question, guarded):
        return _shape_markdown(guarded)
    denial = "This is a prospective July scenario, not observed post-relocation earnings and not a guaranteed result."
    if guarded.strip().endswith("**"):
        guarded = guarded[:-2].rstrip() + " " + denial + "**"
    else:
        guarded = guarded.rstrip() + "\n\n- " + denial
    return _shape_markdown(guarded)


def _needs_denial(question: str, prose: str) -> bool:
    named = re.search(r"july scenario|forecast", prose, re.IGNORECASE) is not None
    if _ASKS_GUARANTEE.search(question) and (not re.search(r"not guaranteed", prose, re.IGNORECASE) or not named):
        return True
    if _ASKS_EARNINGS.search(question) and (not _DENIES_REALIZED.search(prose) or not named):
        return True
    return False


def _stray_numbers(answer: str, packet_text: str) -> tuple[str, ...]:
    packet_numbers = {_plain(token) for token in re.findall(r"\d[\d,]*(?:\.\d+)?", packet_text)}
    stray = []
    for token in re.findall(r"\d[\d,]*(?:\.\d+)?%?", answer):
        plain = _plain(token.rstrip("%"))
        if plain in packet_numbers or _rounded_in_packet(plain, packet_numbers):
            continue
        stray.append(token)
    return tuple(dict.fromkeys(stray))


def _plain(token: str) -> str:
    return token.replace(",", "").replace("%", "")


def _rounded_in_packet(plain: str, packet_numbers: set[str]) -> bool:
    try:
        value = float(plain)
    except ValueError:
        return False
    for packed in packet_numbers:
        try:
            other = float(packed)
        except ValueError:
            continue
        if other == value:
            return True
        if abs(other) >= 100 and abs(other - value) < 1:
            return True
        if packed.startswith(plain) and "." in packed:
            return True
    return False


def _public_error(exc: BaseException, secret: str) -> str:
    code = getattr(exc, "code", None)
    text = str(exc)
    if secret:
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"AIza[\w\-]+", "[redacted]", text)
    text = re.sub(r"AQ\.[\w\-]+", "[redacted]", text)
    if code == 429 or "RESOURCE_EXHAUSTED" in text or "rate" in text.lower():
        return "Gemini is rate-limiting requests. Wait a moment and try one question. The other pages still work."
    if code in (408, 504) or "timeout" in text.lower() or "timed out" in text.lower():
        return "Gemini did not answer in time. The other pages still work."
    if code in (401, 403) or "PERMISSION_DENIED" in text or "API key" in text:
        return "Gemini refused the credential. Ask ante is unavailable. The other pages still work."
    if code == 404:
        return "No cost-conscious Gemini model accepted the request. Ask ante is unavailable. The other pages still work."
    return "Gemini is temporarily unavailable. The other pages still work."
