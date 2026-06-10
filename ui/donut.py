"""Asset-class allocation donut chart."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from analytics.portfolio import SchemeRow
from ui.format import TYPE_DISPLAY, fmt_inr


def breakdown_data(rows: list[SchemeRow], type_filter: str) -> tuple[pd.DataFrame, str]:
    """Return (chart-ready df, title). When ``type_filter == "ALL"`` the chart
    is grouped by asset class; otherwise by sub-category within that class."""
    bucket = (lambda r: TYPE_DISPLAY.get(r.type, r.type)) if type_filter == "ALL" else (lambda r: r.sub_type)
    df = pd.DataFrame(
        [{"Bucket": bucket(r), "Current": r.current_value} for r in rows if r.current_value > 0],
        columns=["Bucket", "Current"],
    ).groupby("Bucket", as_index=False)["Current"].sum()
    title = "Asset class" if type_filter == "ALL" else f"{TYPE_DISPLAY.get(type_filter, type_filter)} sub-categories"
    return df, title


def render_donut(df: pd.DataFrame, title: str, show_value: bool = False,
                 key: str | None = None) -> None:
    """Render an asset/inflow donut. ``key`` keeps multiple donuts on one page
    distinct (e.g. the planner's category donut and its per-fund drill-down)."""
    df = df[df["Current"] > 0].copy()
    if df.empty:
        st.info("Nothing to chart for this filter.")
        return

    total = df["Current"].sum()
    df = df.sort_values("Current", ascending=False).copy()
    df["Pct"] = df["Current"] / total * 100

    custom_data = list(zip(df["Current"].apply(fmt_inr), df["Pct"].round(2)))

    # The inflow chart wants the rupee figure on the slice itself (amount, then
    # share); the portfolio chart keeps the leaner label-and-percent.
    text_tmpl = ("<b>%{label}</b><br>%{customdata[0]}<br>%{percent}"
                 if show_value else "<b>%{label}</b><br>%{percent}")

    fig = go.Figure(
        data=[
            go.Pie(
                labels=df["Bucket"].tolist(),
                values=df["Current"].tolist(),
                hole=0.45,
                sort=False,
                direction="clockwise",
                rotation=90,
                textposition="outside",
                texttemplate=text_tmpl,
                textfont=dict(size=13),
                hovertemplate="<b>%{label}</b><br>%{customdata[0]} (%{percent})<extra></extra>",
                customdata=custom_data,
                marker=dict(line=dict(color="#1e1e1e", width=2)),
                automargin=True,
            )
        ]
    )
    fig.update_layout(
        height=460,
        margin=dict(t=20, b=20, l=20, r=20),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        uniformtext=dict(minsize=10, mode="hide"),
    )

    st.plotly_chart(fig, use_container_width=True, key=key)
