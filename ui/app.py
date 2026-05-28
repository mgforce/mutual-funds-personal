"""
Streamlit dashboard for the personal MF portfolio.

This process renders ONLY the authenticated dashboard. Login, signup, setup,
migration, link-account, change-password, delete-account all live in the
FastAPI gateway (auth_server/). The gateway reverse-proxies authenticated
requests here, injecting a signed X-Session-Payload header that we read
to identify the user and access their CAS data keys.

Run via the gateway:
  streamlit run ui/app.py
  uvicorn auth_server.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from analytics import auth  # noqa: E402
from analytics.accounts import AccountContext, account_context  # noqa: E402
from analytics.demo import is_demo_slug  # noqa: E402
from analytics.nav import get_latest_nav  # noqa: E402
from analytics.portfolio import (  # noqa: E402
    SchemeRow, combined_xirr, filter_rows, latest_pdf_enc, parse_cas, to_scheme_rows,
)
from analytics.tax import EQUITY_LTCG_EXEMPTION  # noqa: E402

from ui.auth_glue import active_slug, consume_flash, session_from_header  # noqa: E402
from ui.cas_workflow import render_cas_workflow  # noqa: E402
from ui.donut import breakdown_data, render_donut  # noqa: E402
from ui.format import TYPE_DISPLAY, fmt_inr, fmt_pct  # noqa: E402
from ui.query import clear_query_keep_account  # noqa: E402
from ui.scheme_card import (  # noqa: E402
    CARD_CSS, SORT_DEFAULT_ASC, SORT_KEYS, render_scheme_card, render_sort_header,
)
from ui.scheme_detail import fy_realized_equity_ltcg, render_scheme_detail  # noqa: E402
from ui.sidebar import render_account_picker  # noqa: E402
from ui.systematic_view import render_systematic  # noqa: E402

st.set_page_config(page_title="Mutual Fund Portfolio", layout="wide")


# ---------------------------------------------------------------------------
# Cached loaders
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner="Fetching latest NAV from AMFI…")
def cached_nav():
    try:
        return get_latest_nav()
    except Exception as e:
        st.warning(f"Could not fetch AMFI NAV (using CAS valuation instead): {e}")
        return {}


@st.cache_data(show_spinner="Parsing CAS PDF…")
def cached_load(slug: str, _ctx: AccountContext) -> tuple[list[SchemeRow], str, str]:
    """Cache key is slug only (Streamlit's leading-underscore convention tells
    it to skip hashing _ctx — its data_key bytes don't survive logout anyway)."""
    cas = parse_cas(_ctx)
    rows = to_scheme_rows(cas, nav_lookup=cached_nav())
    investor = (cas.get("investor_info") or {}).get("name") or "—"
    period_to = (cas.get("statement_period") or {}).get("to") or "—"
    return rows, investor, str(period_to)


def _reset_caches() -> None:
    """Drop both parsed-CAS and AMFI-NAV caches. Called after account switch,
    settings save, fresh PDF arrival, dev re-parse, etc."""
    cached_load.clear()
    cached_nav.clear()


# ---------------------------------------------------------------------------
# Query-string handling: ?scheme= is persistent (detail page), ?sort= is
# transient (apply, then strip so refresh doesn't re-flip).
# ---------------------------------------------------------------------------

def _apply_sort_param() -> None:
    qp = st.query_params
    if "sort" not in qp:
        return
    new_key = qp.get("sort") or "value"
    cur_key = st.session_state.get("_sort_key", "value")
    cur_asc = st.session_state.get("_sort_asc", SORT_DEFAULT_ASC["value"])
    if new_key == cur_key:
        st.session_state["_sort_asc"] = not cur_asc
    else:
        st.session_state["_sort_key"] = new_key
        st.session_state["_sort_asc"] = SORT_DEFAULT_ASC.get(new_key, False)
    scheme = qp.get("scheme")
    clear_query_keep_account()
    if scheme:
        st.query_params["scheme"] = scheme
    st.rerun()


# ---------------------------------------------------------------------------
# Top-level filters (asset class + sub-category + AMC + active/total)
# ---------------------------------------------------------------------------

FILTER_LABELS = {
    "ALL": "All",
    "EQUITY": "Equity",
    "DEBT": "Debt",
    "MULTI_ASSET": "Multi Asset",
    "FOREIGN": "Foreign Funds",
}


def _render_filters(rows: list[SchemeRow]) -> tuple[list[SchemeRow], list[SchemeRow], str]:
    """Returns (type_filtered_full, visible_after_all_filters, type_filter_key).
    type_filtered_full is what the donut chart uses; visible is the schemes list."""
    view_choice = st.segmented_control(
        "View",
        options=["Active funds", "Total funds"],
        default="Active funds",
        key="view_mode_seg",
        help="Active = currently held only. Total = includes fully-redeemed funds and their lifetime XIRR.",
    ) or "Active funds"
    show_redeemed = view_choice == "Total funds"

    label_to_key = {v: k for k, v in FILTER_LABELS.items()}
    chosen_label = st.segmented_control(
        "Asset class",
        options=list(FILTER_LABELS.values()),
        default="All",
        key="type_filter_seg",
    ) or "All"
    type_filter = label_to_key[chosen_label]

    type_filtered = filter_rows(rows, type_filter)

    # AMC multi-select. Empty selection = include all.
    all_amcs = sorted({r.amc for r in type_filtered if r.amc})
    selected_amcs = st.multiselect(
        "AMC",
        options=all_amcs,
        default=[],
        placeholder="All AMCs",
        key=f"amc_filter_{type_filter}",
        help="Filter by Asset Management Company. Leave empty to include all.",
    )
    if selected_amcs:
        type_filtered = [r for r in type_filtered if r.amc in selected_amcs]

    plan_choice = st.segmented_control(
        "Plan",
        options=["All", "Direct", "Regular"],
        default="All",
        key=f"plan_filter_{type_filter}",
    ) or "All"
    if plan_choice != "All":
        type_filtered = [r for r in type_filtered if r.plan_type == plan_choice]

    # Sub-category: only meaningful inside a specific asset class.
    available_subs = sorted({
        TYPE_DISPLAY.get(r.type, r.type) if type_filter == "ALL" else r.sub_type
        for r in type_filtered if show_redeemed or r.current_value > 0
    })
    sub_choice = "All"
    if type_filter != "ALL" and len(available_subs) > 1:
        sub_options = ["All"] + available_subs
        sub_key = f"sub_seg_{type_filter}"
        if st.session_state.get(sub_key) not in sub_options:
            st.session_state[sub_key] = "All"
        sub_choice = st.segmented_control(
            f"{FILTER_LABELS[type_filter]} sub-category",
            options=sub_options,
            key=sub_key,
        ) or "All"

    def matches_sub(r: SchemeRow) -> bool:
        if sub_choice == "All":
            return True
        if type_filter == "ALL":
            return TYPE_DISPLAY.get(r.type, r.type) == sub_choice
        return r.sub_type == sub_choice

    sub_filtered = [r for r in type_filtered if matches_sub(r)]
    visible = sub_filtered if show_redeemed else [r for r in sub_filtered if r.current_value > 0]
    return type_filtered, visible, type_filter


def _render_summary_metrics(visible: list[SchemeRow]) -> None:
    invested = sum(r.invested for r in visible)
    current = sum(r.current_value for r in visible)
    gain = current - invested
    gain_pct = (gain / invested) if invested else 0.0
    f_xirr = combined_xirr(visible)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Current value", fmt_inr(current))
    c2.metric("Invested", fmt_inr(invested))
    c3.metric("Absolute gain", fmt_inr(gain), f"{gain_pct*100:.2f}%")
    xirr_color = "🟢" if (f_xirr or 0) >= 0 else "🔴"
    c4.metric("XIRR", f"{xirr_color} {fmt_pct(f_xirr)}")


def _render_capital_gains(all_rows: list[SchemeRow]) -> None:
    """Always over the full portfolio — the ₹1.25L cap is per-FY across all
    equity LTCG, independent of the current view's filters."""
    fy_realized, fy_start, fy_end = fy_realized_equity_ltcg(all_rows)
    fy_remaining = max(0.0, EQUITY_LTCG_EXEMPTION - fy_realized)
    fy_label = f"FY {fy_start.year}-{str(fy_start.year + 1)[-2:]}"

    st.subheader(f"Capital Gains ({fy_label})")
    st.caption(
        f"Apr {fy_start.year} → {fy_end.strftime('%d %b %Y')} · "
        f"Equity LTCG cap ₹{EQUITY_LTCG_EXEMPTION:,}/yr"
    )
    c1, c2 = st.columns(2)
    c1.metric("Realized Equity LTCG", fmt_inr(fy_realized))
    c2.metric("LTCG room remaining", fmt_inr(fy_remaining))


def _render_scheme_list(visible: list[SchemeRow]) -> None:
    if not visible:
        st.info("No schemes match the current filter.")
        return
    st.subheader(f"Schemes ({len(visible)})")
    st.caption("Tap any card to see folios, per-folio XIRR, and transactions.")
    st.markdown(CARD_CSS, unsafe_allow_html=True)

    render_sort_header()
    sort_key = st.session_state.get("_sort_key", "value")
    sort_asc = st.session_state.get("_sort_asc", False)
    visible = sorted(visible, key=SORT_KEYS[sort_key], reverse=not sort_asc)

    portfolio_total = sum(r.current_value for r in visible) or 1.0
    for r in visible:
        render_scheme_card(r, allocation_pct=r.current_value / portfolio_total * 100)


# ---------------------------------------------------------------------------
# CAS loading
# ---------------------------------------------------------------------------

def _load_cas(slug: str, ctx: AccountContext) -> tuple[list[SchemeRow], str, str, Path | None, bool]:
    """Returns (rows, investor, period_to, enc_pdf, pdf_password_mismatch).
    Empty rows + no enc_pdf when the account has no CAS yet; mismatch flag
    when the PDF password doesn't decrypt the on-disk file."""
    try:
        enc_pdf = latest_pdf_enc(ctx)
    except FileNotFoundError:
        return [], "—", "—", None, False

    try:
        rows, investor, period_to = cached_load(slug, ctx)
        return rows, investor, period_to, enc_pdf, False
    except Exception as e:
        # casparser raises IncorrectPasswordError; check by name so we don't
        # import its module unconditionally.
        if type(e).__name__ == "IncorrectPasswordError":
            cached_load.clear()
            return [], "—", "—", enc_pdf, True
        raise


def _render_pdf_mismatch_recovery(ctx: AccountContext) -> None:
    st.error(
        "The CAS PDF on disk was encrypted with a different password than "
        "we have stored for this account — most likely a stale email from "
        "an earlier setup got picked up. Delete it and request a fresh one."
    )
    if st.button("🗑️ Delete cached PDF", type="primary"):
        for f in ctx.cas_dir.glob("*.pdf.enc"):
            f.unlink()
        if ctx.parse_cache_path.exists():
            ctx.parse_cache_path.unlink()
        _reset_caches()
        st.rerun()


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def main() -> None:
    session = session_from_header()
    if session is None:
        # Reachable only when someone hits :8501 directly, bypassing the gateway.
        st.error(
            "This dashboard must be accessed through the auth gateway. "
            "Open **http://localhost:8000** instead of :8501."
        )
        st.stop()

    # Streamlit's top-right hamburger exposes cache-clear / rerun / dev options
    # that aren't meant for regular users. Show it to admins only.
    if not session.is_admin:
        st.markdown(
            "<style>[data-testid='stToolbar']{display:none!important;}</style>",
            unsafe_allow_html=True,
        )

    consume_flash()
    _apply_sort_param()
    st.session_state["_selected_scheme_id"] = st.query_params.get("scheme") or None

    st.title("Mutual Fund Portfolio")

    slug = active_slug(session)
    if slug is None:
        st.info("You don't have any CAS accounts yet.")
        return
    if is_demo_slug(slug):
        st.warning(
            "🧪 **Demo account — none of the data shown below is real.** "
            "Holdings, transactions, NAVs and XIRRs are all fictional, "
            "served from a hand-crafted sample CAS so visitors can see how "
            "the dashboard works. To track your own portfolio, ask the "
            "admin for an invite."
        )
    ctx = account_context(session, slug)

    rows, investor, period_to, enc_pdf, pdf_password_mismatch = _load_cas(slug, ctx)
    has_cas = enc_pdf is not None and not pdf_password_mismatch

    # "Valued as of" — newest of any scheme's nav_date that we displayed
    nav_dates = sorted({r.nav_date for r in rows if r.current_value > 0})
    valued_as_of = max(nav_dates) if nav_dates else "—"
    nav_sources = {r.nav_source for r in rows if r.current_value > 0}

    with st.sidebar:
        render_account_picker(session, on_account_change=_reset_caches)
        if is_demo_slug(slug):
            st.caption("🧪 _Demo account — sample data, not real._")
        st.divider()

        if has_cas:
            st.markdown(f"**Investor**\n\n{investor}")
            st.markdown(f"**CAS as of**\n\n{period_to}")
            st.markdown(f"**Valued as of**\n\n{valued_as_of}  \n_({', '.join(sorted(nav_sources)) or '—'})_")
            st.caption(f"PDF: `{enc_pdf.name}`")
        else:
            st.info("No CAS yet for this account. Use the buttons below to fetch one.")

        st.divider()
        render_cas_workflow(ctx, slug, enc_pdf, _reset_caches)

    if pdf_password_mismatch:
        _render_pdf_mismatch_recovery(ctx)
        return

    if not has_cas:
        st.info("📥 No CAS PDF for this account yet — click **🔄 Refresh CAS** in the sidebar "
                "to fetch your first statement (CAMS will email it within 5-30 min). "
                "Then click **📥 Process inbox** to load it.")
        return

    # Detail-only view: when a scheme is selected, hide the list/filters/chart.
    selected_id = st.session_state.get("_selected_scheme_id")
    if selected_id:
        selected_row = next((r for r in rows if (r.isin or r.scheme) == selected_id), None)
        if selected_row is None:
            clear_query_keep_account()
        else:
            if st.button("← Back to portfolio", key="back_to_list"):
                clear_query_keep_account()
                st.rerun()
            render_scheme_detail(selected_row, rows)
            return

    tab_portfolio, tab_systematic = st.tabs(["📊 Portfolio", "🔁 SIPs & STPs"])

    with tab_portfolio:
        type_filtered, visible, _type_filter = _render_filters(rows)
        _render_summary_metrics(visible)
        st.divider()
        _render_capital_gains(rows)
        st.divider()
        _render_scheme_list(visible)
        st.divider()

        # Chart always shows the full type-level breakdown.
        df_chart, title = breakdown_data(type_filtered, _type_filter)
        st.subheader(title)
        render_donut(df_chart, title)

    with tab_systematic:
        render_systematic(rows)


if __name__ == "__main__":
    main()
