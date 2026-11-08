#shared streamlit pieces. pages stay scripts and call these helpers.
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ante.snapshot import GoldError, SnapshotGold, open_gold

ROOT = Path(__file__).resolve().parents[1]


@st.cache_resource(show_spinner="Checking the Gold snapshot…")
def gold() -> SnapshotGold:
    return open_gold(ROOT)


def frame(name: str) -> pd.DataFrame:
    try:
        return gold().frame(name)
    except GoldError as exc:
        st.error(str(exc), title="Gold snapshot could not be verified")
        st.stop()
        raise


def money(value, digits: int = 0) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"${float(value):,.{digits}f}"


def signed_percent(value, digits: int = 1) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):+.{digits}%}"


def percent(value, digits: int = 1) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):.{digits}%}"


def number(value, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.{digits}f}"


def install_type() -> None:
    #local faces. theme.fontFaces also registers them for native headings.
    st.html(
        """
        <style>
        /* Local Streamlit serves these at /app/static. Community Cloud mounts the
           app at /~/+/, and a root /app/static request returns the host page
           instead of the file. The first source that decodes is the one used. */
        @font-face {
            font-family: "Redaction35";
            src: url("/app/static/fonts/Redaction35-Regular.ttf") format("truetype"),
                 url("/~/+/app/static/fonts/Redaction35-Regular.ttf") format("truetype");
            font-weight: 400;
            font-style: normal;
            font-display: swap;
        }
        @font-face {
            font-family: "Redaction35";
            src: url("/app/static/fonts/Redaction35-Bold.ttf") format("truetype"),
                 url("/~/+/app/static/fonts/Redaction35-Bold.ttf") format("truetype");
            font-weight: 700;
            font-style: normal;
            font-display: swap;
        }
        @font-face {
            font-family: "Redaction35";
            src: url("/app/static/fonts/Redaction35-Italic.ttf") format("truetype"),
                 url("/~/+/app/static/fonts/Redaction35-Italic.ttf") format("truetype");
            font-weight: 400;
            font-style: italic;
            font-display: swap;
        }
        h1, h2, h3, h4, h5, h6 {
            font-family: "Redaction35", Georgia, serif;
            font-weight: 400;
        }
        /* kpi values: Redaction 35 Bold. size tracks the card so long figures stay inside it. */
        [data-testid="stMetric"] {
            container-type: inline-size;
        }
        [data-testid="stMetricValue"],
        [data-testid="stMetricValue"] * {
            font-family: "Redaction35", Georgia, serif !important;
            font-weight: 700;
            font-style: normal;
            letter-spacing: -0.02em;
            font-size: min(1.85rem, 11.1cqi) !important;
        }
        [data-testid="stMetricLabel"],
        [data-testid="stMetricLabel"] * {
            font-family: Manrope, "Source Sans", sans-serif !important;
            font-weight: 500;
        }
        </style>
        """
    )


def brand() -> None:
    st.html(
        """
        <style>
        .ante-mast { margin: 0 0 1.25rem; }
        .ante-mark {
            font-family: "Redaction35", Georgia, serif;
            font-weight: 400;
            font-style: normal;
            font-size: 5.5rem;
            line-height: 0.8;
            letter-spacing: -0.035em;
            color: #f3efe4;
            margin: 0;
        }
        .ante-place {
            font-family: Manrope, sans-serif;
            margin: 0.9rem 0 0;
            letter-spacing: 0.24em;
            text-transform: uppercase;
            font-size: 0.72rem;
            font-weight: 500;
            color: #c4a35a;
        }
        .ante-role {
            font-family: "Redaction35", Georgia, serif;
            font-style: italic;
            font-weight: 400;
            margin: 0.15rem 0 0;
            font-size: 1.2rem;
            color: #f3efe4;
        }
        </style>
        <div class="ante-mast">
            <p class="ante-mark">ante</p>
            <p class="ante-place">The Gilded Ace</p>
            <p class="ante-role">Floor strategy</p>
        </div>
        """
    )


def coming_next(title: str, lead: str, bullets: list[str]) -> None:
    st.badge("Coming next", color="orange", icon=":material/schedule:")
    st.header(title)
    st.markdown(lead)
    st.info(
        "This page is not built yet. It does not show analysis, charts, or figures.",
        title="Incomplete",
        icon=":material/pending:",
    )
    with st.container(border=True):
        st.markdown("**Planned Gold sources**")
        for bullet in bullets:
            st.markdown(f"- {bullet}")
        st.caption("Nothing here is a result. The overview and Machine Performance pages are the working sections.")
