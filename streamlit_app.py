#ante entry point. navigation only; page bodies live in app_pages.
from __future__ import annotations

import streamlit as st

from ante.ask_chat import ask_dock
from ante.snapshot import GoldError
from ante.ui import gold, install_type

st.set_page_config(
    page_title="ante",
    page_icon=":material/playing_cards:",
    layout="wide",
    initial_sidebar_state="expanded",
)

pages = [
    st.Page(
        "app_pages/overview.py",
        title="Overview",
        icon=":material/dashboard:",
        default=True,
        help=(
            "Floor results for the selected dates, and the July investment scenario. "
            "Observed win and theoretical scenarios are labeled separately."
        ),
    ),
    st.Page(
        "app_pages/machine_performance.py",
        title="Machine Performance",
        icon=":material/playing_cards:",
        url_path="machines",
        help=(
            "Six-month machine rankings, the M-1158 inspection lead, and performance "
            "recomputed for a date, zone, or game filter."
        ),
    ),
    st.Page(
        "app_pages/demand.py",
        title="Demand & Location",
        icon=":material/pin_drop:",
        url_path="demand",
        help=(
            "Zone and property demand, the Back Forty walkway closure, and what four past "
            "relocations did and did not show."
        ),
    ),
    st.Page(
        "app_pages/downtime.py",
        title="Downtime & Operations",
        icon=":material/handyman:",
        url_path="downtime",
        help=(
            "Availability, downtime by cause and place, estimated lost play, and why the "
            "maintenance options are not in the July plan."
        ),
    ),
    st.Page(
        "app_pages/players.py",
        title="Players & Loyalty",
        icon=":material/groups:",
        url_path="players",
        help="Carded play, wagering concentration, and comps. Aggregates only, with no player records.",
    ),
    st.Page(
        "app_pages/investment.py",
        title="Investment Decision",
        icon=":material/account_balance:",
        url_path="investment",
        help="The July plan: $6,600 of spend, the unspent balance, and the alternatives that were not funded.",
    ),
    st.Page(
        "app_pages/model_validation.py",
        title="Model Validation",
        icon=":material/query_stats:",
        url_path="validation",
        help="The frozen May–June forecast. The weekday baseline beat ridge on the holdout.",
    ),
]

page = st.navigation(pages)

try:
    gold()
except GoldError as exc:
    st.error(str(exc), title="Gold snapshot could not be verified")
    st.stop()

install_type()

with st.sidebar:
    st.header("ante", anchor=False)

page.run()
ask_dock(page.title)
