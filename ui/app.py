"""
Streamlit UI for the personal MF portfolio.

Run with:  streamlit run ui/app.py
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analytics.accounts import (  # noqa: E402
    active_account_slug,
    delete_account,
    get_account,
    list_accounts,
    set_active,
    slugify,
    upsert_account,
)
from analytics.portfolio import (  # noqa: E402
    FolioEntry,
    SchemeRow,
    combined_xirr,
    filter_rows,
    latest_pdf,
    parse_cas,
    to_scheme_rows,
)
from analytics.nav import get_latest_nav  # noqa: E402
from analytics.state import load_state, update_state  # noqa: E402
from analytics.tax import (  # noqa: E402
    DEBT_LTCG, DEBT_SLAB, EQ_LTCG, EQ_STCG, EQUITY_LTCG_EXEMPTION,
    build_open_lots, current_fy_window, realized_ltcg_in_window,
    simulate_redemption,
)

st.set_page_config(page_title="Mutual Fund Portfolio", layout="wide")


@st.cache_data(show_spinner="Fetching latest NAV from AMFI…")
def cached_nav():
    try:
        return get_latest_nav()
    except Exception as e:
        st.warning(f"Could not fetch AMFI NAV (using CAS valuation instead): {e}")
        return {}


@st.cache_data(show_spinner="Parsing CAS PDF…")
def load() -> tuple[list[SchemeRow], str, str]:
    cas = parse_cas()
    nav_lookup = cached_nav()
    rows = to_scheme_rows(cas, nav_lookup=nav_lookup)
    investor = (cas.get("investor_info") or {}).get("name") or "—"
    period_to = (cas.get("statement_period") or {}).get("to") or "—"
    return rows, investor, str(period_to)


def fmt_inr(amount: float | None) -> str:
    """Indian-locale formatting: 25974239 → '₹2,59,74,239'."""
    if amount is None or pd.isna(amount):
        return "—"
    n = abs(int(round(amount)))
    sign = "-" if amount < 0 else ""
    s = str(n)
    if len(s) <= 3:
        return f"{sign}₹{s}"
    last3 = s[-3:]
    rest = s[:-3]
    groups: list[str] = []
    while len(rest) > 2:
        groups.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.append(rest)
    groups.reverse()
    return f"{sign}₹{','.join(groups)},{last3}"


def fmt_pct(x: float | None) -> str:
    return f"{x*100:.2f}%" if x is not None else "—"


def color_signed(val):
    if val is None or pd.isna(val):
        return ""
    return "color: #16a34a; font-weight: 600" if val > 0 else "color: #dc2626; font-weight: 600"


TYPE_DISPLAY = {
    "EQUITY": "Equity",
    "DEBT": "Debt",
    "MULTI_ASSET": "Multi Asset",
    "FOREIGN": "Foreign Funds",
    "HYBRID": "Hybrid",
    "OTHER": "Other",
}


def breakdown_data(rows: list[SchemeRow], type_filter: str) -> tuple[pd.DataFrame, str]:
    """Return (chart-ready df, title) for the appropriate breakdown."""
    if type_filter == "ALL":
        df = (
            pd.DataFrame([
                {"Bucket": TYPE_DISPLAY.get(r.type, r.type), "Current": r.current_value}
                for r in rows if r.current_value > 0
            ])
            .groupby("Bucket", as_index=False)["Current"].sum()
        )
        return df, "Asset class"
    df = (
        pd.DataFrame([{"Bucket": r.sub_type, "Current": r.current_value} for r in rows if r.current_value > 0])
        .groupby("Bucket", as_index=False)["Current"].sum()
    )
    return df, f"{TYPE_DISPLAY.get(type_filter, type_filter)} sub-categories"


def render_donut(df: pd.DataFrame, title: str) -> None:
    df = df[df["Current"] > 0].copy()
    if df.empty:
        st.info("Nothing to chart for this filter.")
        return

    total = df["Current"].sum()
    df = df.sort_values("Current", ascending=False).copy()
    df["Pct"] = df["Current"] / total * 100

    custom_data = list(zip(
        df["Current"].apply(fmt_inr),
        df["Pct"].round(2),
    ))

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
                texttemplate="<b>%{label}</b><br>%{percent}",
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
        showlegend=True,
        legend=dict(
            title=dict(text=title),
            orientation="h",
            yanchor="top", y=-0.05,
            xanchor="center", x=0.5,
            font=dict(size=12),
        ),
        uniformtext=dict(minsize=10, mode="hide"),
    )

    st.plotly_chart(fig, use_container_width=True)


_AMC_PALETTE = [
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444",
    "#8b5cf6", "#06b6d4", "#ec4899", "#84cc16",
    "#f97316", "#14b8a6", "#a855f7", "#0ea5e9",
]


def _amc_initials(name: str) -> str:
    parts = (name or "?").strip().split()
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return (name[:2] if len(name) >= 2 else name or "?").upper()


def _amc_color(name: str) -> str:
    if not name:
        return _AMC_PALETTE[0]
    return _AMC_PALETTE[sum(ord(c) for c in name) % len(_AMC_PALETTE)]


_CARD_CSS = """
<style>
.mf-card, .mf-sort-header {
  max-width: 1400px;
}
.mf-card {
  border: 1px solid rgba(128,128,128,0.25);
  border-radius: 10px;
  padding: 12px 16px;
  margin-bottom: 8px;
}
.mf-card-selected { border: 1.5px solid #f59e0b; }
.mf-card-row {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 16px;
}
.mf-card-icon {
  width: 36px; height: 36px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  color: white; font-weight: 700; font-size: 13px; flex-shrink: 0;
}
.mf-card-name { flex: 1; min-width: 0; }
.mf-card-name-line { font-weight: 600; line-height: 1.3; }
.mf-card-meta { font-size: 0.78em; opacity: 0.7; margin-top: 3px; }
.mf-card-numbers {
  flex: 3;
  display: flex;
  flex-direction: row;
  gap: 16px;
}
.mf-card-cell { flex: 1; min-width: 0; text-align: right; }
.mf-card-label { font-size: 0.72em; opacity: 0.6; margin-top: 2px; }
.mf-sort-header { padding: 6px 16px; margin-bottom: 4px; }
.mf-sort-header .mf-card-icon { background: none !important; visibility: hidden; }
.mf-sort-link {
  text-decoration: none !important;
  color: inherit !important;
  font-size: 0.95em;
  opacity: 0.85;
  font-weight: 600;
}
.mf-sort-link.active { opacity: 1; font-weight: 700; }
.mf-sort-chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 4px 0 12px; }
@media (max-width: 700px) {
  .mf-sort-header { display: none !important; }
  .mf-card-row { flex-direction: column; align-items: stretch; gap: 10px; }
  .mf-card-icon { display: none; }
  .mf-card-numbers { justify-content: space-between; gap: 12px; }
  .mf-card-cell { min-width: 0; text-align: left; }
  .mf-card-cell.mid { text-align: center; }
  .mf-card-cell.right { text-align: right; }
}
@media (min-width: 701px) {
  .mf-sort-chips { display: none !important; }
}
</style>
"""


def render_scheme_card(
    r: SchemeRow,
    is_selected: bool,
    allocation_pct: float,
) -> None:
    """Responsive card: single row + icon on desktop; name + numbers stacked on mobile."""
    folio_word = "folio" if len(r.folios) == 1 else "folios"
    xirr_str = f"{r.xirr*100:.2f}%" if r.xirr is not None else "—"
    xirr_color = "#22c55e" if (r.xirr or 0) >= 0 else "#ef4444"
    gain_color = "#22c55e" if r.gain >= 0 else "#ef4444"
    href = "?scheme=" if is_selected else f"?scheme={r.isin or r.scheme}"
    selected_cls = " mf-card-selected" if is_selected else ""
    icon_letter = _amc_initials(r.amc or r.scheme)
    icon_color = _amc_color(r.amc or r.scheme)

    st.markdown(
        f"""
        <a href="{href}" target="_self"
           style="text-decoration:none;color:inherit;display:block;">
          <div class="mf-card{selected_cls}">
            <div class="mf-card-row">
              <div class="mf-card-icon" style="background:{icon_color};">{icon_letter}</div>
              <div class="mf-card-name">
                <div class="mf-card-name-line">{r.scheme}</div>
                <div class="mf-card-meta">
                  {r.sub_type} · {len(r.folios)} {folio_word} · NAV ₹{r.nav:,.2f}
                </div>
              </div>
              <div class="mf-card-numbers">
                <div class="mf-card-cell">
                  <div style="font-weight:600;">{fmt_inr(r.current_value)}</div>
                  <div class="mf-card-label">{allocation_pct:.1f}% of MF</div>
                </div>
                <div class="mf-card-cell mid">
                  <div>{fmt_inr(r.invested)}</div>
                  <div class="mf-card-label">Invested</div>
                </div>
                <div class="mf-card-cell right">
                  <div style="color:{xirr_color};font-weight:600;">{xirr_str}</div>
                  <div class="mf-card-label" style="color:{gain_color};opacity:1;">
                    {r.gain_pct*100:+.1f}%
                  </div>
                </div>
              </div>
            </div>
          </div>
        </a>
        """,
        unsafe_allow_html=True,
    )


_SORT_KEYS = {
    "name":     lambda r: (r.scheme or "").lower(),
    "value":    lambda r: r.current_value,
    "invested": lambda r: r.invested,
    "xirr":     lambda r: float("-inf") if r.xirr is None else r.xirr,
}
_SORT_DEFAULT_ASC = {"name": True, "value": False, "invested": False, "xirr": False}


def render_sort_header() -> None:
    """Renders both the desktop column-aligned header and the mobile chip row.
    CSS media queries hide whichever doesn't apply for the current viewport."""
    sort_key = st.session_state.get("_sort_key", "value")
    sort_asc = st.session_state.get("_sort_asc", _SORT_DEFAULT_ASC["value"])

    def link(label: str, key: str) -> str:
        is_active = sort_key == key
        arrow = (" ↑" if sort_asc else " ↓") if is_active else ""
        cls = "mf-sort-link active" if is_active else "mf-sort-link"
        return f'<a href="?sort={key}" target="_self" class="{cls}">{label}{arrow}</a>'

    # Desktop: column-aligned header that mirrors the card layout.
    st.markdown(
        f"""
        <div class="mf-sort-header">
          <div class="mf-card-row">
            <div class="mf-card-icon"></div>
            <div class="mf-card-name">{link("Name", "name")}</div>
            <div class="mf-card-numbers">
              <div class="mf-card-cell">{link("Value", "value")}</div>
              <div class="mf-card-cell mid">{link("Invested", "invested")}</div>
              <div class="mf-card-cell right">{link("XIRR", "xirr")}</div>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Mobile: pill chips (column-aligned header is hidden by media query).
    chips = []
    for key, label in [("name", "Name"), ("value", "Value"),
                       ("invested", "Invested"), ("xirr", "XIRR")]:
        is_active = sort_key == key
        arrow = (" ↑" if sort_asc else " ↓") if is_active else ""
        bg = "rgba(245,158,11,0.18)" if is_active else "rgba(128,128,128,0.12)"
        weight = "600" if is_active else "400"
        chips.append(
            f'<a href="?sort={key}" target="_self" '
            f'style="text-decoration:none;color:inherit;background:{bg};'
            f'padding:4px 12px;border-radius:14px;font-weight:{weight};'
            f'font-size:0.85em;display:inline-block;">{label}{arrow}</a>'
        )
    st.markdown(
        f"""
        <div class="mf-sort-chips">
          <span style="opacity:0.65;font-size:0.85em;">Sort by:</span>
          {"".join(chips)}
        </div>
        """,
        unsafe_allow_html=True,
    )


def fy_realized_equity_ltcg(all_rows: list[SchemeRow]) -> tuple[float, date, date]:
    """Sum equity LTCG already realized in the current FY across all schemes."""
    fy_start, fy_end = current_fy_window()
    total = 0.0
    for sch in all_rows:
        if sch.type != "EQUITY":
            continue
        for f in sch.folio_details:
            total += realized_ltcg_in_window(f.transactions, fy_start, fy_end)
    return total, fy_start, fy_end


def render_redemption_calculator(r: SchemeRow, all_rows: list[SchemeRow]) -> None:
    """Inline LTCG/STCG split for a hypothetical redemption.
    Sources transactions from all folios in the scheme and runs FIFO."""
    st.markdown("### 💰 Redemption gain breakdown")

    all_tx: list[dict] = []
    for f in r.folio_details:
        all_tx.extend(f.transactions)
    open_lots = build_open_lots(all_tx)
    available_units = sum(l.units for l in open_lots)

    if available_units <= 0 or r.nav <= 0:
        st.info("No open units to redeem (or NAV unavailable).")
        return

    available_value = available_units * r.nav

    # Long-term threshold differs: equity = 12 months, debt = 24 months.
    default_treat = "Equity" if r.type in ("EQUITY", "MULTI_ASSET") else "Debt"
    treat_options = ["Equity", "Debt"]
    treat_choice = st.radio(
        "Long-term threshold",
        options=treat_options,
        index=treat_options.index(default_treat),
        horizontal=True,
        key=f"tax_treat_{r.isin or r.scheme}",
        help="Equity = 12 months, Debt = 24 months. Pick based on the fund's "
             "actual equity composition (≥65% Indian equity → Equity).",
    )
    is_equity = treat_choice == "Equity"

    cols = st.columns([2, 1])
    with cols[0]:
        amount = st.number_input(
            "Amount to redeem (₹)",
            min_value=0.0,
            max_value=float(available_value),
            value=float(available_value),
            step=10000.0,
            format="%.0f",
            key=f"redeem_amt_{r.isin or r.scheme}",
            help=f"Max: {fmt_inr(available_value)} ({available_units:,.4f} units @ ₹{r.nav:,.4f})",
        )
    with cols[1]:
        st.metric("Available", fmt_inr(available_value))

    units_to_redeem = amount / r.nav
    res = simulate_redemption(
        open_lots,
        redeem_units=units_to_redeem,
        current_nav=r.nav,
        is_equity=is_equity,
    )

    ltcg_total = res.bucket_gain.get(EQ_LTCG, 0.0) + res.bucket_gain.get(DEBT_LTCG, 0.0)
    stcg_total = res.bucket_gain.get(EQ_STCG, 0.0) + res.bucket_gain.get(DEBT_SLAB, 0.0)
    threshold_label = "12 months" if is_equity else "24 months"

    st.markdown(
        f"""
- Sale value &nbsp; **{fmt_inr(res.sale_value)}**
- Amount invested (FIFO) &nbsp; **{fmt_inr(res.cost_basis)}**
- Total gain &nbsp; **{fmt_inr(res.total_gain)}**
    - LTCG (held >{threshold_label}) &nbsp; **{fmt_inr(ltcg_total)}**
    - STCG (held ≤{threshold_label}) &nbsp; **{fmt_inr(stcg_total)}**
"""
    )

    # FY exemption tracker (only meaningful for equity LTCG).
    if is_equity:
        realized, fy_start, fy_end = fy_realized_equity_ltcg(all_rows)
        ltcg_after = realized + max(0.0, ltcg_total)
        remaining_now = max(0.0, EQUITY_LTCG_EXEMPTION - realized)
        remaining_after = max(0.0, EQUITY_LTCG_EXEMPTION - ltcg_after)
        excess = max(0.0, ltcg_after - EQUITY_LTCG_EXEMPTION)

        st.markdown(
            f"""
**Equity LTCG exemption — FY {fy_start.strftime('%b %Y')} → {fy_end.strftime('%d %b %Y')}** &nbsp;(₹{EQUITY_LTCG_EXEMPTION:,}/yr cap)
- Already realized this FY (across all your equity funds) &nbsp; **{fmt_inr(realized)}**
- LTCG room remaining today &nbsp; **{fmt_inr(remaining_now)}**
- If you proceed with this redemption &nbsp; **{fmt_inr(remaining_after)}** room left
"""
        )
        if excess > 0:
            st.caption(
                f"⚠️ This redemption would push you ₹{excess:,.0f} over the "
                f"₹{EQUITY_LTCG_EXEMPTION:,} exemption — that excess is taxable LTCG."
            )

    # Lot-by-lot breakdown
    with st.expander(f"📋 Lot-by-lot breakdown ({len(res.breakdown)} lots)", expanded=False):
        lot_rows = [{
            "Purchase date": b.lot_date,
            "Units": b.units,
            "Cost": b.cost,
            "Sale": b.sale,
            "Gain": b.gain,
            "Days held": b.days_held,
            "Type": "LTCG" if b.bucket in (EQ_LTCG, DEBT_LTCG) else "STCG",
        } for b in res.breakdown]
        if lot_rows:
            ldf = pd.DataFrame(lot_rows)
            lstyled = (
                ldf.style
                .format({
                    "Cost": fmt_inr,
                    "Sale": fmt_inr,
                    "Gain": fmt_inr,
                    "Units": "{:,.4f}",
                })
                .map(color_signed, subset=["Gain"])
            )
            st.dataframe(lstyled, use_container_width=True, hide_index=True)


def render_scheme_detail(r: SchemeRow, _all_rows_for_detail: list[SchemeRow]) -> None:
    """Drawer-style detail panel shown below the schemes table."""
    st.divider()
    header_cols = st.columns([6, 1])
    with header_cols[0]:
        st.subheader(r.scheme)
        st.caption(f"{r.amc} · {r.sub_type} · ISIN {r.isin or '—'}")
    with header_cols[1]:
        if st.button("Close", use_container_width=True, key=f"close_detail_{r.isin or r.scheme}"):
            st.query_params.clear()
            st.rerun()

    cols = st.columns(4)
    cols[0].metric("Invested", fmt_inr(r.invested))
    cols[1].metric("Current", fmt_inr(r.current_value))
    gain_color = "🟢" if r.gain >= 0 else "🔴"
    cols[2].metric("Gain", f"{gain_color} {fmt_inr(r.gain)}", f"{r.gain_pct*100:.2f}%")
    xirr_color = "🟢" if (r.xirr or 0) >= 0 else "🔴"
    cols[3].metric("XIRR", f"{xirr_color} {fmt_pct(r.xirr)}")

    nav_str = f"₹{r.nav:.4f}" if r.nav else "—"
    st.caption(f"Units held: {r.units:,.4f}  ·  NAV {nav_str} ({r.nav_source}, {r.nav_date})")

    # Folios
    st.markdown(f"**Folios ({len(r.folio_details)})**")
    folio_df = pd.DataFrame([{
        "Folio": f.folio,
        "Name": f.holder_name or "—",
        "Invested": f.invested,
        "Current": f.current_value,
        "Units": f.units,
        "Gain (₹)": f.gain,
        "Gain %": f.gain_pct * 100,
        "XIRR %": (f.xirr * 100) if f.xirr is not None else None,
        "Txns": len(f.transactions),
    } for f in r.folio_details])

    folio_styled = (
        folio_df.style
        .format({
            "Invested": fmt_inr,
            "Current": fmt_inr,
            "Gain (₹)": fmt_inr,
            "Units": "{:,.4f}",
            "Gain %": "{:.2f}%",
            "XIRR %": lambda v: "—" if pd.isna(v) else f"{v:.2f}%",
        })
        .map(color_signed, subset=["Gain (₹)", "Gain %", "XIRR %"])
    )
    st.dataframe(folio_styled, use_container_width=True, hide_index=True)

    # Redemption tax calculator (toggle).
    tax_open_key = f"tax_open_{r.isin or r.scheme}"
    if st.button(
        "💰 Calculate gain on redemption",
        key=f"tax_btn_{r.isin or r.scheme}",
        help="Show LTCG/STCG split of the gain if you redeem this scheme.",
    ):
        st.session_state[tax_open_key] = not st.session_state.get(tax_open_key, False)
    if st.session_state.get(tax_open_key, False):
        render_redemption_calculator(r, _all_rows_for_detail)

    # Transactions (collapsed)
    total_tx = sum(len(f.transactions) for f in r.folio_details)
    with st.expander(f"📜 Transactions ({total_tx})", expanded=False):
        rows_tx = []
        for f in r.folio_details:
            for t in f.transactions:
                rows_tx.append({
                    "Date": t["date"],
                    "Folio": f.folio,
                    "Type": t["type"],
                    "Amount (₹)": t.get("amount"),
                    "Units": t.get("units"),
                    "NAV": t.get("nav"),
                    "Balance units": t.get("balance"),
                    "Description": t.get("description") or "",
                })
        if not rows_tx:
            st.caption("No transactions on file.")
        else:
            tx_df = pd.DataFrame(rows_tx).sort_values("Date", ascending=False)
            tx_styled = (
                tx_df.style
                .format({
                    "Amount (₹)": lambda v: fmt_inr(v) if v is not None and not pd.isna(v) else "—",
                    "Units": lambda v: f"{v:,.4f}" if v is not None and not pd.isna(v) else "—",
                    "NAV": lambda v: f"{v:,.4f}" if v is not None and not pd.isna(v) else "—",
                    "Balance units": lambda v: f"{v:,.4f}" if v is not None and not pd.isna(v) else "—",
                })
                .map(color_signed, subset=["Amount (₹)"])
            )
            st.dataframe(tx_styled, use_container_width=True, hide_index=True)


@st.dialog("Add account")
def add_account_dialog() -> None:
    st.caption("Each account keeps its own CAS PDFs and analytics in isolation.")
    email = st.text_input("Email (will be used as account name)")
    pdf_password = st.text_input("PDF password (encrypts the CAS emailed to you)", type="password")
    app_password = st.text_input("Gmail App Password", type="password",
                                 help="Generate one at myaccount.google.com/apppasswords")
    from_date = st.date_input("Statement from-date", value=date(2014, 1, 1))

    cols = st.columns([1, 1])
    with cols[0]:
        if st.button("Save", type="primary", use_container_width=True):
            if not email.strip():
                st.error("Email is required.")
                return
            slug = slugify(email.strip())
            upsert_account(
                slug,
                email=email.strip(),
                pdf_password=pdf_password,
                app_password=app_password.replace(" ", ""),
                from_date=from_date.isoformat(),
            )
            set_active(slug)
            load.clear()
            cached_nav.clear()
            st.rerun()
    with cols[1]:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


def render_account_picker() -> None:
    """Sidebar account selector + add/edit/delete."""
    accounts = list_accounts()
    active = active_account_slug()

    options = [slug for slug, _ in accounts]
    label_for = {slug: email for slug, email in accounts}

    chosen = st.selectbox(
        "Account",
        options=options,
        index=options.index(active) if active in options else 0,
        format_func=lambda s: label_for.get(s, s),
        key="acc_picker",
    )
    if chosen != active:
        set_active(chosen)
        load.clear()
        cached_nav.clear()
        st.rerun()

    if st.button("➕ Add account", use_container_width=True):
        add_account_dialog()

    with st.expander("⚙️ Settings (this account)"):
        info = get_account(active)
        new_email = st.text_input("Email", value=info.get("email", ""), key="set_email")
        new_pdf = st.text_input("PDF password", value=info.get("pdf_password", ""),
                                type="password", key="set_pdf")
        new_app = st.text_input("Gmail App Password", value=info.get("app_password", ""),
                                type="password", key="set_app")
        try:
            from_default = date.fromisoformat(info.get("from_date") or "2014-01-01")
        except Exception:
            from_default = date(2014, 1, 1)
        new_from = st.date_input("From date", value=from_default, key="set_from")

        c1, c2 = st.columns([1, 1])
        with c1:
            if st.button("💾 Save", type="primary", use_container_width=True, key="set_save"):
                upsert_account(
                    active,
                    email=new_email.strip(),
                    pdf_password=new_pdf,
                    app_password=new_app.replace(" ", ""),
                    from_date=new_from.isoformat(),
                )
                load.clear()
                cached_nav.clear()
                st.toast("Settings saved", icon="💾")
                st.rerun()
        with c2:
            if len(accounts) > 1:
                if st.button("🗑️ Delete", use_container_width=True, key="set_delete"):
                    delete_account(active)
                    load.clear()
                    cached_nav.clear()
                    st.rerun()


def main() -> None:
    # ?scheme=ISIN is *persistent* — it identifies the detail page, so we
    # mirror it into session_state but leave the URL untouched (refresh,
    # share, browser-back all work). ?sort=KEY is *transient* — apply the
    # toggle, then strip it (preserving ?scheme=) so a refresh doesn't
    # re-flip the sort direction.
    qp = st.query_params
    if "sort" in qp:
        new_key = qp.get("sort") or "value"
        cur_key = st.session_state.get("_sort_key", "value")
        cur_asc = st.session_state.get("_sort_asc", _SORT_DEFAULT_ASC["value"])
        if new_key == cur_key:
            st.session_state["_sort_asc"] = not cur_asc
        else:
            st.session_state["_sort_key"] = new_key
            st.session_state["_sort_asc"] = _SORT_DEFAULT_ASC.get(new_key, False)
        scheme = qp.get("scheme")
        st.query_params.clear()
        if scheme:
            st.query_params["scheme"] = scheme
        st.rerun()
    st.session_state["_selected_scheme_id"] = qp.get("scheme") or None

    st.title("Mutual Fund Portfolio")

    has_cas = True
    pdf = None
    rows: list[SchemeRow] = []
    investor = "—"
    period_to = "—"
    try:
        pdf = latest_pdf()
        rows, investor, period_to = load()
    except FileNotFoundError:
        has_cas = False

    # Compute "valued as of" — earliest of any scheme's nav_date that we displayed
    nav_dates = sorted({r.nav_date for r in rows if r.current_value > 0})
    valued_as_of = max(nav_dates) if nav_dates else "—"
    nav_sources = {r.nav_source for r in rows if r.current_value > 0}

    state = load_state()

    with st.sidebar:
        render_account_picker()
        st.divider()

        if has_cas:
            st.markdown(f"**Investor**\n\n{investor}")
            st.markdown(f"**CAS as of**\n\n{period_to}")
            st.markdown(f"**Valued as of**\n\n{valued_as_of}  \n_({', '.join(sorted(nav_sources)) or '—'})_")
            st.caption(f"PDF: `{pdf.name}`")
        else:
            st.info("No CAS yet for this account. Use the buttons below to fetch one.")

        st.divider()
        st.markdown("### CAS workflow")

        last_req = state.get("last_request_at")
        last_fetched_at = state.get("last_fetched_at")
        # If we already pulled the email since the last request, don't show
        # a stale "waiting" reminder.
        awaiting = False
        if last_req:
            try:
                t_req = datetime.fromisoformat(last_req)
                t_fetch = datetime.fromisoformat(last_fetched_at) if last_fetched_at else None
                awaiting = (t_fetch is None) or (t_fetch < t_req)
                mins = int((datetime.now() - t_req).total_seconds() // 60)
                hrs, mins_remain = divmod(mins, 60)
                ago = f"{hrs}h {mins_remain}m" if hrs else f"{mins_remain} min"
                if awaiting:
                    st.warning(
                        f"⏳ Refresh CAS requested **{ago} ago** "
                        f"({t_req.strftime('%d %b, %H:%M')}).  \n"
                        "Email arrives in 5-15 min (up to 1h). "
                        "Click **Process inbox** once it lands."
                    )
                else:
                    st.caption(f"✅ Last fetched email {ago} ago.")
            except Exception:
                st.caption(f"Last requested: {last_req}")
        else:
            st.caption("Click 'Refresh CAS' to fetch a fresh statement.")

        if st.button("🔄 Refresh CAS", use_container_width=True,
                     help="Submit a fresh CAS request to CAMS. They'll email the PDF in 5-30 minutes."):
            from ingest.cams_request import submit_via_playwright
            from ingest.gmail_fetch import (
                fetch_latest_cas,
                load_config as _gmail_cfg,
                peek_latest_uid,
            )
            import time

            gmail_cfg = _gmail_cfg()
            try:
                baseline_uid = peek_latest_uid(gmail_cfg)
            except Exception:
                baseline_uid = None

            with st.spinner("Submitting CAS request to CAMS… (~30s)"):
                result = submit_via_playwright(force=True, headless=True)

            if not (result.get("ok") and result.get("submitted")):
                st.error(f"Submission failed: {result.get('error', 'unknown')}")
            else:
                update_state(last_request_at=datetime.now().isoformat())
                st.toast("✅ Submitted to CAMS — waiting for email.", icon="📨")

                poll_seconds = 600
                interval = 30
                deadline = time.time() + poll_seconds
                progress = st.progress(0.0, text="Waiting for CAMS email…")
                arrived_path = None
                while time.time() < deadline:
                    elapsed = poll_seconds - (deadline - time.time())
                    mins, secs = divmod(int(elapsed), 60)
                    progress.progress(
                        min(elapsed / poll_seconds, 1.0),
                        text=f"Waiting for CAMS email… {mins}m {secs:02d}s elapsed",
                    )
                    time.sleep(interval)
                    try:
                        new_uid = peek_latest_uid(gmail_cfg)
                    except Exception:
                        continue
                    if new_uid and new_uid != baseline_uid:
                        try:
                            arrived_path = fetch_latest_cas(gmail_cfg)
                        except Exception as e:
                            st.error(f"Email arrived but fetch failed: {e}")
                        break
                progress.empty()

                if arrived_path is not None:
                    update_state(
                        last_fetched_pdf=str(arrived_path),
                        last_fetched_at=datetime.now().isoformat(),
                    )
                    load.clear()
                    cached_nav.clear()
                    st.success(f"✅ Got new CAS: {arrived_path.name}")
                    st.rerun()
                else:
                    st.warning(
                        "⏳ Email hasn't arrived after 10 minutes. CAMS sometimes "
                        "takes longer — click **Process inbox** once it lands."
                    )

        if st.button("📥 Process inbox", use_container_width=True,
                     help="Look for the latest CAMS email, download the PDF, and refresh the dashboard."):
            from ingest.gmail_fetch import fetch_latest_cas, NoNewCasError, load_config as _gmail_cfg
            try:
                with st.spinner("Checking inbox…"):
                    new_path = fetch_latest_cas(_gmail_cfg())
                # `pdf` was captured at top of main(); on a fresh account it's
                # None. If the inbox produced a new file, latest_pdf() now
                # resolves to a different name (or to a name at all).
                if pdf is None or new_path.name != pdf.name:
                    update_state(last_fetched_pdf=str(new_path),
                                 last_fetched_at=datetime.now().isoformat())
                    load.clear()
                    cached_nav.clear()
                    st.success(f"✅ Got new CAS: {new_path.name}")
                    st.rerun()
                else:
                    st.info("No newer CAS email yet — try again in a few minutes.")
            except NoNewCasError as e:
                st.warning(str(e))
            except Exception as e:
                st.error(f"Fetch failed: {e}")

        if st.button("📊 Re-parse current PDF with latest NAV", use_container_width=True,
                     help="Re-fetch today's NAV from AMFI and recompute valuations + XIRR. Skips the PDF parser (fast)."):
            from analytics.nav import NAV_CACHE
            if NAV_CACHE.exists():
                NAV_CACHE.unlink()
            cached_nav.clear()
            load.clear()
            st.rerun()

        st.divider()
        # TODO: remove once classification/categorization rules are stable.
        if st.button("🧹 Re-parse current PDF (dev)", use_container_width=True,
                     help="Force re-parse the current CAS even if cached. Use after editing classification rules."):
            from analytics.portfolio import parse_cache_path
            p = parse_cache_path()
            if p.exists():
                p.unlink()
            load.clear()
            cached_nav.clear()
            st.rerun()

    if not has_cas:
        st.info("📥 No CAS PDF for this account yet — click **🔄 Refresh CAS** in the sidebar "
                "to fetch your first statement (CAMS will email it within 5-30 min). "
                "Then click **📥 Process inbox** to load it.")
        return

    # Detail-only view: when a scheme is selected, hide the list/filters/chart
    # and render only that scheme. Back button restores the portfolio view —
    # filter state lives in session_state so it survives the round trip.
    selected_id = st.session_state.get("_selected_scheme_id")
    if selected_id:
        selected_row = next(
            (r for r in rows if (r.isin or r.scheme) == selected_id),
            None,
        )
        if selected_row is None:
            st.query_params.clear()
        else:
            if st.button("← Back to portfolio", key="back_to_list"):
                st.query_params.clear()
                st.rerun()
            render_scheme_detail(selected_row, rows)
            return

    FILTER_LABELS = {
        "ALL": "All",
        "EQUITY": "Equity",
        "DEBT": "Debt",
        "MULTI_ASSET": "Multi Asset",
        "FOREIGN": "Foreign Funds",
    }
    label_to_key = {v: k for k, v in FILTER_LABELS.items()}

    # View toggle: Active (held only) vs Total (includes fully-redeemed funds).
    view_choice = st.segmented_control(
        "View",
        options=["Active funds", "Total funds"],
        default="Active funds",
        key="view_mode_seg",
        help="Active = currently held only. Total = includes fully-redeemed funds and their lifetime XIRR.",
    ) or "Active funds"
    show_redeemed = view_choice == "Total funds"

    # Top-level filter.
    chosen_label = st.segmented_control(
        "Asset class",
        options=list(FILTER_LABELS.values()),
        default="All",
        key="type_filter_seg",
    ) or "All"
    type_filter = label_to_key[chosen_label]

    type_filtered = filter_rows(rows, type_filter)

    # AMC filter — multi-select across whatever AMCs exist in the asset-class
    # filtered subset. Empty selection = include all.
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

    # Sub-category options: in Total view we also surface sub-types that exist
    # only among redeemed schemes (current_value == 0).
    available_subs = sorted({
        TYPE_DISPLAY.get(r.type, r.type) if type_filter == "ALL" else r.sub_type
        for r in type_filtered if show_redeemed or r.current_value > 0
    })

    sub_choice = "All"
    # Sub-category control only makes sense after picking a specific asset
    # class (and only when there's actually variety to filter through).
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
    # In Active view: hide fully-redeemed schemes everywhere. The metric XIRR
    # then matches what's in the table.
    # In Total view: include redeemed schemes — their lifetime cashflows
    # contribute to the combined XIRR shown up top.
    if show_redeemed:
        visible = list(sub_filtered)
    else:
        visible = [r for r in sub_filtered if r.current_value > 0]

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

    # Equity LTCG exemption banner — always over the full portfolio (not the
    # filtered view), since the ₹1.25L cap is per-FY across all your equity LTCG.
    fy_realized, fy_start, fy_end = fy_realized_equity_ltcg(rows)
    fy_remaining = max(0.0, EQUITY_LTCG_EXEMPTION - fy_realized)
    st.markdown(
        f"📅 **Equity LTCG this FY** (Apr {fy_start.year} → {fy_end.strftime('%d %b %Y')}): "
        f"realized **{fmt_inr(fy_realized)}** &nbsp;·&nbsp; "
        f"tax-free room remaining **{fmt_inr(fy_remaining)}** of "
        f"₹{EQUITY_LTCG_EXEMPTION:,}"
    )

    st.divider()
    if not visible:
        st.info("No schemes match the current filter.")
    else:
        st.subheader(f"Schemes ({len(visible)})")
        st.caption("Tap any card to see folios, per-folio XIRR, and transactions.")
        st.markdown(_CARD_CSS, unsafe_allow_html=True)

        # Sort header (clickable chips).
        render_sort_header()
        sort_key = st.session_state.get("_sort_key", "value")
        sort_asc = st.session_state.get("_sort_asc", False)
        visible = sorted(visible, key=_SORT_KEYS[sort_key], reverse=not sort_asc)

        portfolio_total = sum(r.current_value for r in visible) or 1.0

        for r in visible:
            allocation = r.current_value / portfolio_total * 100
            render_scheme_card(r, is_selected=False, allocation_pct=allocation)

    st.divider()

    # Chart always shows the full type-level breakdown so user can see all
    # sub-categories at a glance.
    df_chart, title = breakdown_data(type_filtered, type_filter)
    st.subheader(title)
    render_donut(df_chart, title)


if __name__ == "__main__":
    main()
