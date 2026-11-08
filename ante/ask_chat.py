#floating ask ante. a fragment, so opening it does not rerun the page or call gemini.
from __future__ import annotations

import streamlit as st

from ante.analyst import SESSION_LIMIT, AnalystError, ask
from ante.evidence import NO_EVIDENCE, refusal, retrieve
from ante.ui import gold

SUGGESTED = (
    "Why shouldn't we replace the bottom ten?",
    "Why inspect M-1158?",
    "Why did the weekday forecast beat ridge?",
)


def _key() -> str:
    try:
        value = st.secrets["GEMINI_API_KEY"]
    except Exception:
        return ""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _ensure() -> None:
    st.session_state.setdefault("ask_messages", [])
    st.session_state.setdefault("ask_count", 0)
    st.session_state.setdefault("ask_cache", {})
    st.session_state.setdefault("ask_panel", False)


def _labels(sources: list[dict]) -> list[str]:
    labels = []
    for source in sources:
        label = source["source"]
        if label not in labels:
            labels.append(label)
    return labels


def _complete(viewing: str) -> None:
    messages = st.session_state.ask_messages
    question = messages[-1]["text"]
    earlier = [item["text"] for item in messages[:-1] if item["role"] == "user"]
    prior = earlier[-1] if earlier else ""
    history = [
        {"role": item["role"], "text": item["text"]}
        for item in messages[:-1]
        if not item.get("error") and not item.get("local")
    ]
    try:
        skipped = refusal(question, prior)
        if skipped:
            messages.append({"role": "assistant", "text": skipped, "local": True})
            return
        packet = retrieve(gold(), question, prior)
        if packet.blocked:
            messages.append({
                "role": "assistant",
                "text": "ante will not ask Gemini how to target an individual guest from losses.",
                "local": True,
            })
            return
        if packet.insufficient or not packet.facts:
            messages.append({"role": "assistant", "text": NO_EVIDENCE, "local": True})
            return
        cache_key = "brief\n" + question + "\n" + prior
        cached = st.session_state.ask_cache.get(cache_key)
        if cached is None:
            secret = _key()
            if not secret:
                raise AnalystError("Gemini is not configured. The rest of ante still works.")
            if st.session_state.ask_count >= SESSION_LIMIT:
                raise AnalystError("This session has reached 30 questions. The rest of ante still works.")
            result = ask(
                api_key=secret,
                question=question,
                packet=packet,
                history=history,
                viewing=viewing,
            )
            st.session_state.ask_count += 1
            cached = {
                "text": result.text,
                "sources": [
                    {"source": fact.source, "kind": fact.kind, "text": fact.text}
                    for fact in result.citations
                ],
                "stray": list(result.stray_numbers),
                "insufficient": result.insufficient,
            }
            st.session_state.ask_cache[cache_key] = cached
        messages.append({"role": "assistant", **cached})
    except AnalystError as exc:
        messages.append({"role": "assistant", "text": str(exc), "error": True})
    except Exception:
        messages.append({
            "role": "assistant",
            "text": "Gemini is temporarily unavailable. The rest of ante still works.",
            "error": True,
        })


def _styles() -> None:
    st.html(
        """
        <style>
        .st-key-ask_dock {
            position: fixed !important;
            right: 1.05rem !important;
            bottom: 50px !important;
            z-index: 1000002 !important;
            margin: 0 !important;
        }
        .st-key-ask_dock:not(:has(.ask-panel)) {
            width: auto !important;
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            padding: 0 !important;
        }
        .st-key-ask_dock:has(.ask-panel) {
            width: min(400px, calc(100vw - 1.5rem)) !important;
            max-height: min(68vh, 620px);
            overflow: auto;
            background: #161d2e;
            border: 1px solid #33415c;
            border-radius: 10px;
            box-shadow: 0 16px 40px rgba(0, 0, 0, 0.4);
            padding: 0.75rem 0.85rem 0.6rem !important;
        }
        .ask-title {
            font-family: "Redaction35", Georgia, serif;
            font-weight: 400;
            font-size: 1.7rem;
            line-height: 0.9;
            letter-spacing: -0.03em;
            color: #f3efe4;
            margin: 0;
        }
        .st-key-ask_launcher button {
            position: relative !important;
            display: inline-flex !important;
            align-items: center;
            flex-wrap: nowrap;
            gap: 0.45rem;
            width: max-content !important;
            height: auto !important;
            min-height: 0 !important;
            padding: 0.38rem 0.9rem !important;
            border-radius: 999px !important;
            background: #10182a !important;
            border: 1px solid #c4a35a !important;
            box-shadow: 0 8px 22px rgba(0, 0, 0, 0.28);
            color: transparent !important;
        }
        .st-key-ask_launcher button > * {
            position: absolute !important;
            width: 1px !important;
            height: 1px !important;
            overflow: hidden !important;
            clip: rect(0 0 0 0) !important;
        }
        .st-key-ask_launcher button::before,
        .st-key-ask_launcher button::after {
            white-space: nowrap;
            flex: 0 0 auto;
        }
        .st-key-ask_launcher button::before {
            content: "Ask ante";
            font-family: "Redaction35", Georgia, serif;
            font-weight: 400;
            font-size: 1.2rem;
            letter-spacing: -0.03em;
            line-height: 1;
            color: #f3efe4;
        }
        .st-key-ask_launcher button::after {
            content: "·  Powered by Gemini";
            font-family: Manrope, "Source Sans", sans-serif;
            font-weight: 500;
            font-size: 0.68rem;
            letter-spacing: 0.01em;
            line-height: 1;
            color: #c4a35a;
        }
        .st-key-ask_launcher button:hover {
            background: #1c263c !important;
            border-color: #e0c98a !important;
        }
        .st-key-ask_dismiss button {
            background: transparent;
            color: #f3efe4;
            border: 1px solid #33415c;
            border-radius: 999px;
            padding: 0.15rem 0.55rem;
            min-height: 2rem;
        }
        [class*="st-key-asku"] {
            border-left: 2px solid #c4a35a;
            padding: 0.15rem 0 0.15rem 0.6rem;
            margin: 0.45rem 0 0.2rem;
        }
        [class*="st-key-asku"] p,
        [class*="st-key-aska"] p,
        .st-key-ask_thread p {
            font-family: Manrope, "Source Sans", sans-serif;
        }
        [class*="st-key-aska"] { margin: 0.1rem 0 0.55rem; }
        [class*="st-key-aska"] ul {
            margin: 0.2rem 0 0.15rem;
            padding-left: 1.05rem;
        }
        [class*="st-key-aska"] li {
            margin: 0.18rem 0;
            font-family: Manrope, "Source Sans", sans-serif;
            color: #f3efe4;
        }
        [class*="st-key-aska"] strong { font-weight: 650; }
        @media (max-width: 700px) {
            .st-key-ask_dock:has(.ask-panel) {
                right: 0.5rem !important;
                bottom: 50px !important;
                width: calc(100vw - 1rem) !important;
                max-height: min(70vh, calc(100dvh - 8.5rem));
            }
            .st-key-ask_launcher button::before { font-size: 1.02rem; }
            .st-key-ask_launcher button::after { font-size: 0.62rem; }
        }
        </style>
        """
    )


def _draw(viewing: str) -> None:
    messages = st.session_state.ask_messages
    open_panel = st.session_state.ask_panel
    remaining = SESSION_LIMIT - st.session_state.ask_count
    with st.container(key="ask_dock", border=False, width="content"):
        if not open_panel:
            if st.button("Ask ante · Powered by Gemini", key="ask_launcher", help="Ask ante", width="content"):
                st.session_state.ask_panel = True
                st.rerun(scope="fragment")
            return
        with st.container(horizontal=True, vertical_alignment="center"):
            st.html('<p class="ask-title">Ask ante</p><span class="ask-panel"></span>', width="content")
            if st.button("Close", key="ask_dismiss", icon=":material/close:", help="Close"):
                st.session_state.ask_panel = False
                st.rerun(scope="fragment")
        st.caption("Powered by Gemini")
        st.caption(f"{remaining} question{'s' if remaining != 1 else ''} left")
        if not messages:
            st.caption(f"Ask about what is on {viewing}, or start here.")
            for index, suggestion in enumerate(SUGGESTED):
                if st.button(suggestion, key=f"ask_suggest_{index}", width="stretch"):
                    messages.append({"role": "user", "text": suggestion})
                    st.rerun(scope="fragment")
        thread = st.container(height=280, key="ask_thread") if messages else None
        host = thread if thread is not None else st.container()
        with host:
            for index, message in enumerate(messages):
                kind = "asku" if message["role"] == "user" else "aska"
                with st.container(key=f"{kind}{index}"):
                    if message.get("error"):
                        st.error(message["text"], title="Gemini is unavailable")
                        continue
                    st.markdown(message["text"])
                    if message.get("stray"):
                        st.caption("A figure in that reply was not in the retrieved evidence.")
                    sources = message.get("sources") or []
                    if sources:
                        st.caption("  ".join(_labels(sources)))
                        with st.expander("View evidence"):
                            for source in sources:
                                st.markdown(f"**{source['source']}** · {source['kind']}")
                                st.write(source["text"])
        typed = st.chat_input(
            "Ask about the floor",
            key="ask_box",
            max_chars=500,
            disabled=remaining <= 0,
        )
        if typed and typed.strip():
            messages.append({"role": "user", "text": " ".join(typed.split())})
            st.rerun(scope="fragment")


@st.fragment
def ask_dock(viewing: str) -> None:
    _ensure()
    _styles()
    messages = st.session_state.ask_messages
    if st.session_state.ask_panel and messages and messages[-1]["role"] == "user":
        question = messages[-1]["text"]
        earlier = [item["text"] for item in messages[:-1] if item["role"] == "user"]
        prior = earlier[-1] if earlier else ""
        if refusal(question, prior) is None:
            with st.container(key="ask_dock", border=False, width="content"):
                st.html('<p class="ask-title">Ask ante</p><span class="ask-panel"></span>', width="content")
                st.caption("Powered by Gemini")
                with st.spinner("Asking Gemini…"):
                    _complete(viewing)
        else:
            _complete(viewing)
        st.rerun(scope="fragment")
    _draw(viewing)
