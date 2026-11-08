#plotly figures. colors match the ante theme; streamlit still applies its theme pass.
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

GOLD = "#c4a35a"
EMERALD = "#3f8f78"
IVORY = "#f3efe4"
SLATE = "#8ea0b8"
ROSE = "#c46a5a"


def _layout(title: str, height: int = 380) -> dict:
    return dict(
        title=dict(text=title, x=0, xanchor="left", font=dict(size=15, family="Manrope, sans-serif", color=IVORY)),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=IVORY, family="Manrope, sans-serif"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=8, r=8, t=64, b=8),
        height=height,
        hovermode="x unified",
    )


def money_lines(frame: pd.DataFrame, title: str, series: list[tuple[str, str, str]]) -> go.Figure:
    figure = go.Figure()
    for column, label, color in series:
        figure.add_trace(go.Scatter(
            x=frame["gaming_date"],
            y=frame[column],
            name=label,
            mode="lines",
            line=dict(color=color, width=2),
            hovertemplate="%{x|%b %d, %Y}<br>" + label + ": $%{y:,.0f}<extra></extra>",
        ))
    figure.update_layout(**_layout(title))
    figure.update_xaxes(title="Gaming date", showgrid=False)
    figure.update_yaxes(title="USD", tickprefix="$", separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    return figure


def house_versus_theo(bottom: pd.DataFrame) -> go.Figure:
    ordered = bottom.sort_values("list_rank", ascending=False)
    figure = go.Figure()
    figure.add_trace(go.Bar(
        y=ordered["machine_id"],
        x=ordered["house_win_usd"],
        name="Observed house win",
        orientation="h",
        marker_color=IVORY,
        hovertemplate="%{y}<br>House win: $%{x:,.0f}<extra></extra>",
    ))
    figure.add_trace(go.Bar(
        y=ordered["machine_id"],
        x=ordered["theo_usd"],
        name="Theoretical win",
        orientation="h",
        marker_color=GOLD,
        hovertemplate="%{y}<br>Theoretical win: $%{x:,.0f}<extra></extra>",
    ))
    figure.update_layout(**_layout("Raw bottom ten: observed house win and theoretical win", 420), barmode="group")
    figure.update_xaxes(title="USD over the six-month window", tickprefix="$", separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    figure.update_yaxes(title="")
    return figure


def demand_ranks(frame: pd.DataFrame) -> go.Figure:
    #rank 1 is the weakest demand. the line is the cutoff for the bottom ten.
    ordered = frame.sort_values("rank_demand_index", ascending=True)
    figure = go.Figure(go.Bar(
        y=ordered["machine_id"],
        x=ordered["rank_demand_index"],
        orientation="h",
        marker_color=GOLD,
        hovertemplate="%{y}<br>Demand rank %{x:.0f} of 310<extra></extra>",
    ))
    figure.add_vline(x=10, line_width=1, line_dash="dot", line_color=IVORY)
    figure.update_layout(**_layout("Demand rank of the machines with the lowest house win", 420))
    figure.update_xaxes(
        title="Rank among 310 machines. 1 is the weakest demand.",
        range=[0, 320],
        gridcolor="rgba(243,239,228,0.12)",
    )
    figure.update_yaxes(title="")
    return figure


def demand_scatter(scorecard: pd.DataFrame) -> go.Figure:
    frame = scorecard.copy()
    frame["group"] = frame["in_original_bottom_10"].map({True: "Raw bottom ten", False: "Other machines"})
    figure = go.Figure()
    for label, color in (("Other machines", SLATE), ("Raw bottom ten", GOLD)):
        part = frame.loc[frame["group"] == label]
        figure.add_trace(go.Scatter(
            x=part["demand_index"],
            y=part["actual_win_index"],
            mode="markers",
            name=label,
            marker=dict(color=color, size=8 if label == "Raw bottom ten" else 6, opacity=0.9),
            text=part["machine_id"],
            customdata=list(zip(part["primary_zone"], part["theme"], part["house_win_usd"], part["theo_usd"])),
            hovertemplate=(
                "%{text}<br>%{customdata[0]} · %{customdata[1]}"
                "<br>Demand index: %{x:.2f}<br>Actual-win index: %{y:.2f}"
                "<br>House win: $%{customdata[2]:,.0f}<br>Theo: $%{customdata[3]:,.0f}<extra></extra>"
            ),
        ))
    featured = frame.loc[frame["machine_id"] == "M-1158"]
    if not featured.empty:
        row = featured.iloc[0]
        figure.add_annotation(
            x=row["demand_index"],
            y=row["actual_win_index"],
            text="M-1158",
            showarrow=True,
            arrowcolor=ROSE,
            font=dict(color=ROSE),
            ax=40,
            ay=-30,
        )
    figure.update_layout(**_layout("Demand index against actual-win index", 460))
    figure.update_xaxes(title="Demand index (play vs zone and game peers; luck excluded)", gridcolor="rgba(243,239,228,0.12)")
    figure.update_yaxes(title="Actual-win index (includes luck)", gridcolor="rgba(243,239,228,0.12)")
    return figure


ZONE_COLORS = {
    "Back Forty": GOLD,
    "Center Pit": EMERALD,
    "Main Entrance": IVORY,
    "Bar Row": SLATE,
    "Hotel Corridor": ROSE,
    "Garage Entrance": "#7f93ad",
    "High-Limit Room": "#d7c48a",
}
PERIOD_ORDER = ["pre", "closure", "post"]
WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def shade_closure(figure: go.Figure, start, end) -> go.Figure:
    figure.add_vrect(
        x0=start,
        x1=end,
        fillcolor="rgba(196,163,90,0.14)",
        line_width=0,
        layer="below",
    )
    return figure


def zone_rate_lines(frame: pd.DataFrame, title: str, y_column: str, y_title: str) -> go.Figure:
    figure = go.Figure()
    for zone, part in frame.groupby("zone_name", sort=True):
        part = part.sort_values("gaming_date")
        figure.add_trace(go.Scatter(
            x=part["gaming_date"],
            y=part[y_column],
            name=str(zone),
            mode="lines",
            line=dict(color=ZONE_COLORS.get(str(zone), SLATE), width=2),
            hovertemplate="%{x|%b %d, %Y}<br>" + str(zone) + ": %{y:,.0f}<extra></extra>",
        ))
    figure.update_layout(**_layout(title, 420))
    figure.update_xaxes(title="Gaming date", showgrid=False)
    figure.update_yaxes(title=y_title, separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    return figure


def period_bars(frame: pd.DataFrame, title: str, y_column: str, y_title: str) -> go.Figure:
    figure = go.Figure()
    for period, color in zip(PERIOD_ORDER, (SLATE, GOLD, EMERALD)):
        part = frame.loc[frame["closure_period"] == period]
        figure.add_trace(go.Bar(
            x=part["zone_name"],
            y=part[y_column],
            name=period,
            marker_color=color,
            hovertemplate="%{x}<br>" + period + ": %{y:,.0f}<extra></extra>",
        ))
    figure.update_layout(**_layout(title, 420), barmode="group")
    figure.update_xaxes(title="")
    figure.update_yaxes(title=y_title, separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    return figure


def effect_bars(frame: pd.DataFrame, title: str, label_column: str) -> go.Figure:
    #relative-change estimates with their published intervals, shown in percent
    ordered = frame.sort_values("estimate")
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=ordered["estimate"] * 100,
        y=ordered[label_column],
        mode="markers",
        marker=dict(color=GOLD, size=10),
        error_x=dict(
            type="data",
            symmetric=False,
            array=(ordered["ci_high"] - ordered["estimate"]) * 100,
            arrayminus=(ordered["estimate"] - ordered["ci_low"]) * 100,
            color=IVORY,
            thickness=1.4,
        ),
        hovertemplate="%{y}<br>%{x:.1f}%<extra></extra>",
        name="Estimate",
    ))
    figure.add_vline(x=0, line_color=SLATE, line_width=1)
    figure.update_layout(**_layout(title, max(320, 48 * len(ordered) + 80)))
    figure.update_xaxes(title="Relative change, percent", ticksuffix="%", gridcolor="rgba(243,239,228,0.12)")
    figure.update_yaxes(title="")
    return figure


def value_bars(frame: pd.DataFrame, label_column: str, value_column: str, title: str, value_title: str) -> go.Figure:
    ordered = frame.sort_values(value_column, ascending=False)
    figure = go.Figure(go.Bar(
        x=ordered[label_column],
        y=ordered[value_column],
        marker_color=GOLD,
        hovertemplate="%{x}<br>%{y:,.0f}<extra></extra>",
    ))
    figure.update_layout(**_layout(title, 380))
    figure.update_xaxes(title="")
    figure.update_yaxes(title=value_title, separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    return figure


def interval_markers(frame: pd.DataFrame, title: str, label_column: str, x_title: str) -> go.Figure:
    ordered = frame.sort_values("estimate")
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=ordered["estimate"],
        y=ordered[label_column],
        mode="markers",
        marker=dict(color=GOLD, size=10),
        error_x=dict(
            type="data",
            symmetric=False,
            array=ordered["ci_high"] - ordered["estimate"],
            arrayminus=ordered["estimate"] - ordered["ci_low"],
            color=IVORY,
            thickness=1.4,
        ),
        hovertemplate="%{y}<br>%{x:.1f}<extra></extra>",
        name="Rate",
    ))
    figure.update_layout(**_layout(title, max(320, 42 * len(ordered) + 80)))
    figure.update_xaxes(title=x_title, gridcolor="rgba(243,239,228,0.12)")
    figure.update_yaxes(title="")
    return figure


def grouped_bars(frame: pd.DataFrame, x_column: str, series: list[tuple[str, str, str]], title: str, y_title: str) -> go.Figure:
    figure = go.Figure()
    for column, label, color in series:
        figure.add_trace(go.Bar(
            x=frame[x_column],
            y=frame[column],
            name=label,
            marker_color=color,
            hovertemplate="%{x}<br>" + label + ": %{y:,.0f}<extra></extra>",
        ))
    figure.update_layout(**_layout(title, 400), barmode="group")
    figure.update_xaxes(title="")
    figure.update_yaxes(title=y_title, separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    return figure


def allocation_bars(labels: list[str], values: list[float], title: str) -> go.Figure:
    colors = [GOLD if label != "Retained" else SLATE for label in labels]
    figure = go.Figure(go.Bar(
        y=labels,
        x=values,
        orientation="h",
        marker_color=colors,
        hovertemplate="%{y}<br>$%{x:,.0f}<extra></extra>",
    ))
    figure.update_layout(**_layout(title, max(320, 48 * len(labels) + 80)))
    figure.update_xaxes(title="US dollars of the $100,000 budget", tickprefix="$", separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    figure.update_yaxes(title="", categoryorder="array", categoryarray=list(reversed(labels)))
    return figure


def range_bars(frame: pd.DataFrame, label_column: str, title: str, low: str, central: str, high: str) -> go.Figure:
    #published low, central, and high cases. not a confidence interval.
    figure = go.Figure()
    for name, column, color in (("Low", low, SLATE), ("Central", central, GOLD), ("High", high, EMERALD)):
        figure.add_trace(go.Bar(
            y=frame[label_column],
            x=frame[column],
            name=name,
            orientation="h",
            marker_color=color,
            hovertemplate="%{y}<br>" + name + ": $%{x:,.0f}<extra></extra>",
        ))
    figure.update_layout(**_layout(title, max(380, 64 * len(frame) + 90)), barmode="group")
    figure.add_vline(x=0, line_color=SLATE, line_width=1)
    figure.update_xaxes(title="Net theoretical win, USD", tickprefix="$", separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    figure.update_yaxes(title="", categoryorder="array", categoryarray=list(reversed(frame[label_column].tolist())))
    return figure


def dollar_intervals(frame: pd.DataFrame, label_column: str, title: str, x_title: str) -> go.Figure:
    #published estimate and confidence interval, in dollars. not a prediction interval.
    ordered = frame.sort_values("estimate")
    high = (ordered["ci_high"] - ordered["estimate"]).fillna(0)
    low = (ordered["estimate"] - ordered["ci_low"]).fillna(0)
    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=ordered["estimate"],
        y=ordered[label_column],
        mode="markers",
        marker=dict(color=GOLD, size=11),
        error_x=dict(type="data", symmetric=False, array=high, arrayminus=low, color=IVORY, thickness=1.4),
        hovertemplate="%{y}<br>$%{x:,.0f}<extra></extra>",
        name="Estimate",
    ))
    figure.update_layout(**_layout(title, max(280, 56 * len(ordered) + 80)))
    figure.update_xaxes(title=x_title, tickprefix="$", separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    figure.update_yaxes(title="")
    return figure


def forecast_compare(frame: pd.DataFrame, series: list[tuple[str, str, str]], title: str, show_band: bool) -> go.Figure:
    #january-april traces are in-sample fits. may-june traces are the frozen forecast.
    figure = go.Figure()
    training = frame.loc[frame["sample"].astype(str).str.startswith("training")]
    holdout = frame.loc[frame["sample"].astype(str).str.startswith("holdout")]
    figure.add_trace(go.Scatter(
        x=frame["gaming_date"],
        y=frame["actual_coin_in_usd"],
        name="Actual coin-in",
        mode="lines",
        line=dict(color=IVORY, width=2),
        hovertemplate="%{x|%b %d, %Y}<br>Actual: $%{y:,.0f}<extra></extra>",
    ))
    if show_band and not holdout.empty:
        figure.add_trace(go.Scatter(
            x=holdout["gaming_date"],
            y=holdout["forecast_ridge_high_usd"],
            mode="lines",
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
        ))
        figure.add_trace(go.Scatter(
            x=holdout["gaming_date"],
            y=holdout["forecast_ridge_low_usd"],
            name="Ridge nominal 90% prediction interval",
            mode="lines",
            line=dict(width=0),
            fill="tonexty",
            fillcolor="rgba(63,143,120,0.20)",
            hovertemplate="%{x|%b %d, %Y}<br>Interval low: $%{y:,.0f}<extra></extra>",
        ))
    for column, label, color in series:
        if not training.empty:
            figure.add_trace(go.Scatter(
                x=training["gaming_date"],
                y=training[column],
                name=f"{label}, in-sample fit",
                mode="lines",
                line=dict(color=color, width=1.5, dash="dot"),
                hovertemplate="%{x|%b %d, %Y}<br>" + label + " fit: $%{y:,.0f}<extra></extra>",
            ))
        figure.add_trace(go.Scatter(
            x=holdout["gaming_date"],
            y=holdout[column],
            name=f"{label}, frozen forecast",
            mode="lines",
            line=dict(color=color, width=2),
            hovertemplate="%{x|%b %d, %Y}<br>" + label + ": $%{y:,.0f}<extra></extra>",
        ))
    if not training.empty and not holdout.empty:
        figure.add_vrect(
            x0=holdout["gaming_date"].min(),
            x1=pd.Timestamp(holdout["gaming_date"].max()) + pd.Timedelta(days=1),
            fillcolor="rgba(196,163,90,0.08)",
            line_width=0,
            layer="below",
        )
    figure.update_layout(**_layout(title, 460))
    figure.update_xaxes(title="Gaming date", showgrid=False)
    figure.update_yaxes(title="USD of machine coin-in", tickprefix="$", separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    return figure


def cause_bars(labels: list[str], hours: list[float], title: str) -> go.Figure:
    figure = go.Figure(go.Bar(
        x=hours,
        y=labels,
        orientation="h",
        marker_color=GOLD,
        hovertemplate="%{y}<br>%{x:,.0f} hours<extra></extra>",
    ))
    figure.update_layout(**_layout(title, 360))
    figure.update_xaxes(title="Hours", separatethousands=True, gridcolor="rgba(243,239,228,0.12)")
    figure.update_yaxes(title="")
    return figure
