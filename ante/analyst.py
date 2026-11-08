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
- Say whether a figure is observed, a statistical estimate, a forecast, or a July scenario.
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
    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise AnalystError("Gemini returned an empty answer. Nothing was invented in its place.")
    if api_key in text:
        raise AnalystError("The answer was discarded because it echoed a credential.")
    parsed = _parse(text)
    allowed = {fact.fact_id: fact for fact in packet.facts}
    cited = []
    for fact_id in parsed["citations"]:
        fact = allowed.get(str(fact_id).strip())
        if fact is not None and fact not in cited:
            cited.append(fact)
    prose = _shape_markdown(_strip_lead(_strip_unknown_ids(parsed["answer"], set(allowed))))
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


def _parse(raw: str) -> dict:
    body = raw.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?", "", body).strip()
        body = re.sub(r"```$", "", body).strip()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {"answer": raw.strip(), "citations": [], "insufficient": False}
    if not isinstance(parsed, dict):
        return {"answer": raw.strip(), "citations": [], "insufficient": False}
    answer = str(parsed.get("answer") or "").strip()
    citations = parsed.get("citations") or []
    if not isinstance(citations, list):
        citations = []
    if not answer:
        answer = raw.strip()
    return {
        "answer": answer,
        "citations": [str(item) for item in citations],
        "insufficient": bool(parsed.get("insufficient")),
    }


def _strip_unknown_ids(text: str, allowed: set[str]) -> str:
    def replace(match: re.Match) -> str:
        token = match.group(1)
        if token in allowed:
            return ""
        return "" if re.fullmatch(r"E\d+|P[\w\-]+|S\d+|V\d+|B\d+|D\d+", token) else token

    cleaned = re.sub(r"\[?(E\d+|PM-\d+|P\d+|S\d+|V\d+|B\d+|D\d+)\]?", replace, text)
    return "\n".join(" ".join(line.split()) for line in cleaned.splitlines()).strip()


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
