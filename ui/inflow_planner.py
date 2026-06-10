"""Inflow planner tab: pick funds you already hold, enter how much you're
putting in (daily / weekly / monthly), and see the combined monthly inflow
grouped by category as a donut.

Entries are saved per account in ``state.json`` (keyed by slug) so they're
still there next time you log in — edit freely, every change is persisted.
The demo account is read-only, so there the planner stays session-only."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from analytics.portfolio import SchemeRow
from analytics.state import load_state, update_state
from ui.donut import render_donut
from ui.format import fmt_inr

# Monthly-equivalent multipliers: daily SIPs run on ~22 business days/month,
# weekly SIPs fire 52/12 ≈ 4.33 times a month.
FREQ_MULT = {"Monthly": 1.0, "Weekly": 52 / 12, "Daily": 22.0}
FREQ_OPTS = list(FREQ_MULT)

_STATE_KEY = "inflow_planner"


def _fund_id(r: SchemeRow) -> str:
    """Stable id for a scheme — matches app.py's (isin or scheme) convention."""
    return r.isin or r.scheme


def _load_saved(slug: str) -> dict:
    """{fund_id: {"amount": float, "freq": str}} from disk, defensively typed."""
    raw = load_state(slug).get(_STATE_KEY, {})
    out: dict[str, dict] = {}
    if isinstance(raw, dict):
        for fid, v in raw.items():
            if isinstance(v, dict):
                freq = v.get("freq")
                out[fid] = {
                    "amount": float(v.get("amount") or 0.0),
                    "freq": freq if freq in FREQ_MULT else "Monthly",
                }
    return out


def render_inflow_planner(rows: list[SchemeRow], slug: str, is_demo: bool) -> None:
    st.subheader("Plan monthly inflow")
    st.caption(
        "Pick funds you hold, enter how much you invest and how often — daily, "
        "weekly or monthly — and see the combined **monthly** inflow grouped by "
        "category. Daily is converted at 22 business days/month, weekly at "
        "≈4.33 weeks/month. "
        + ("Changes are not saved on the demo account."
           if is_demo else "Your entries are saved and reload next time you log in.")
    )

    if not rows:
        st.info("No funds to plan with yet — load a CAS first.")
        return

    # One row per scheme, sorted by name; keep the first SchemeRow per id for
    # its category (sub_type) and display name.
    by_id: dict[str, SchemeRow] = {}
    for r in sorted(rows, key=lambda r: r.scheme.lower()):
        by_id.setdefault(_fund_id(r), r)

    saved = _load_saved(slug)

    # Seed widget state from the saved plan on first render this session. Once
    # the keys exist, Streamlit drives them and we read the live values back.
    if "planner_funds" not in st.session_state:
        st.session_state["planner_funds"] = [fid for fid in saved if fid in by_id]
    for fid, v in saved.items():
        st.session_state.setdefault(f"planner_amt_{fid}", v["amount"])
        st.session_state.setdefault(f"planner_freq_{fid}", v["freq"])

    selected = st.multiselect(
        "Funds",
        options=list(by_id),
        format_func=lambda fid: f"{by_id[fid].scheme}  ·  {by_id[fid].sub_type}",
        key="planner_funds",
        placeholder="Select one or more funds you hold…",
        help="Only funds in your CAS appear here.",
    )

    if not selected:
        # Persist the cleared plan so it doesn't reappear next login.
        _persist(slug, is_demo, {})
        st.info("Select at least one fund above to start planning.")
        return

    # Per-fund entry rows: name | amount | frequency | monthly equivalent.
    hdr = st.columns([4, 2, 2, 2])
    hdr[0].markdown("**Fund**")
    hdr[1].markdown("**Amount (₹)**")
    hdr[2].markdown("**Frequency**")
    hdr[3].markdown("**Monthly ≈**")

    plan: list[dict] = []
    to_save: dict[str, dict] = {}
    for fid in selected:
        r = by_id[fid]
        st.session_state.setdefault(f"planner_amt_{fid}", 0.0)
        st.session_state.setdefault(f"planner_freq_{fid}", "Monthly")
        c = st.columns([4, 2, 2, 2])
        c[0].markdown(f"{r.scheme}  \n_{r.sub_type}_")
        amount = c[1].number_input(
            "Amount", min_value=0.0, step=500.0,
            key=f"planner_amt_{fid}", label_visibility="collapsed",
        )
        freq = c[2].selectbox(
            "Frequency", FREQ_OPTS, key=f"planner_freq_{fid}",
            label_visibility="collapsed",
        )
        monthly = amount * FREQ_MULT[freq]
        c[3].markdown(fmt_inr(monthly) if monthly else "—")
        to_save[fid] = {"amount": amount, "freq": freq}
        if monthly > 0:
            plan.append({"Category": r.sub_type, "Fund": r.scheme, "Monthly": monthly})

    _persist(slug, is_demo, to_save)

    if not plan:
        st.info("Enter an amount for at least one fund to see the chart.")
        return

    fund_df = pd.DataFrame(plan, columns=["Category", "Fund", "Monthly"])
    total = fund_df["Monthly"].sum()
    st.divider()
    st.metric("Total monthly inflow", fmt_inr(total))

    # The original category donut, unchanged.
    cat_order = (
        fund_df.groupby("Category", as_index=False)["Monthly"].sum()
        .sort_values("Monthly", ascending=False)
    )
    cat_labels = cat_order["Category"].tolist()
    cat_df = cat_order.rename(columns={"Category": "Bucket", "Monthly": "Current"})

    st.subheader("Planned inflow by category")
    render_donut(cat_df, "Planned inflow by category", show_value=True,
                 key="planner_cat_donut")

    # Drill-down via pills, not slice clicks: Streamlit only forwards Plotly
    # box/lasso selections, which a pie has none of, so a category click can't
    # be captured — a row of category buttons drives the breakdown instead.
    st.caption("Tap a category to break it down by fund:")
    selected_cat = st.pills(
        "Break down category", options=cat_labels, selection_mode="single",
        key="planner_drill_cat", label_visibility="collapsed",
    )
    if selected_cat:
        sub = (
            fund_df[fund_df["Category"] == selected_cat]
            .rename(columns={"Fund": "Bucket", "Monthly": "Current"})
            [["Bucket", "Current"]]
        )
        st.subheader(f"{selected_cat} — by fund")
        render_donut(sub, f"{selected_cat} by fund", show_value=True,
                     key="planner_fund_donut")

    # Category summary table.
    table = cat_order.copy()
    table["Share"] = (table["Monthly"] / total * 100).round(2).astype(str) + "%"
    st.dataframe(
        table.style.format({"Monthly": fmt_inr}),
        use_container_width=True, hide_index=True,
    )


def _persist(slug: str, is_demo: bool, plan: dict) -> None:
    """Write the plan to state.json only when it actually changed (avoids a
    disk write on every rerun). No-op on the read-only demo account."""
    if is_demo:
        return
    if load_state(slug).get(_STATE_KEY, {}) != plan:
        update_state(slug, **{_STATE_KEY: plan})
