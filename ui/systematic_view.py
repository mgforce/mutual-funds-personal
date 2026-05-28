"""SIPs & STPs tab: per-month outflow summary plus per-mandate tables."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from analytics.portfolio import SchemeRow
from analytics.systematic import (
    ACTIVE_WINDOW_DAYS, IN_LEG_TYPES, OUT_LEG_TYPES, _is_transfer_leg,
    detect_sips, detect_stps,
)
from ui.format import fmt_inr


def _render_summary(n_sips: int, n_stps: int,
                    monthly_sip: float, monthly_stp: float) -> None:
    total_monthly = monthly_sip + monthly_stp
    st.subheader("Monthly outflow")
    c1, c2, c3 = st.columns(3)
    c1.metric("Total SIP / month", fmt_inr(monthly_sip), f"{n_sips} active")
    c2.metric("Total STP / month", fmt_inr(monthly_stp), f"{n_stps} active")
    c3.metric("Combined / month", fmt_inr(total_monthly), f"{n_sips + n_stps} mandates")
    st.caption(
        f"'Active' = latest installment within {ACTIVE_WINDOW_DAYS} days. "
        "Frequency, monthly equivalent, and the next-expected date are inferred "
        "from the median gap between recent installments — a paused mandate may "
        "show as active until its next slot is missed."
    )


def _render_sip_table(sips: list) -> None:
    st.subheader(f"SIPs ({len(sips)})")
    if not sips:
        st.info("No active SIPs detected.")
        return
    df = pd.DataFrame([{
        "Scheme": s.scheme,
        "Folio": s.folio,
        "Holder": s.holder_name or "—",
        "Amount": s.amount,
        "Frequency": s.frequency_label,
        "Monthly equiv": s.monthly_amount,
        "Installments": s.installments,
        "First": s.first_date,
        "Last": s.last_date,
        "Next expected": s.next_expected,
        "Total invested": s.total_invested,
    } for s in sips])
    styled = df.style.format({
        "Amount": fmt_inr,
        "Monthly equiv": fmt_inr,
        "Total invested": fmt_inr,
    })
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _render_stp_table(stps: list) -> None:
    st.subheader(f"STPs ({len(stps)})")
    if not stps:
        st.info("No active STPs detected.")
        return
    df = pd.DataFrame([{
        "From": s.source_scheme,
        "To": s.target_scheme,
        "Folio": (s.source_folio if s.source_folio == s.target_folio
                  else f"{s.source_folio} → {s.target_folio}"),
        "Holder": s.holder_name or "—",
        "Amount": s.amount,
        "Frequency": s.frequency_label,
        "Monthly equiv": s.monthly_amount,
        "Installments": s.installments,
        "First": s.first_date,
        "Last": s.last_date,
        "Next expected": s.next_expected,
        "Total moved": s.total_amount,
    } for s in stps])
    styled = df.style.format({
        "Amount": fmt_inr,
        "Monthly equiv": fmt_inr,
        "Total moved": fmt_inr,
    })
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _render_debug_panel(rows: list[SchemeRow]) -> None:
    """Dump every candidate STP leg from the last 90 days. Useful when an STP
    you know exists isn't being detected — you can spot whether the
    transactions are actually in the CAS, and what type / amount / date /
    description they carry."""
    cutoff = date.today() - timedelta(days=90)
    leg_rows: list[dict] = []
    for r in rows:
        for f in r.folio_details:
            for t in f.transactions:
                ttype = t.get("type")
                tdate = t.get("date")
                if not tdate or tdate < cutoff:
                    continue
                if ttype not in OUT_LEG_TYPES and ttype not in IN_LEG_TYPES:
                    continue
                leg_rows.append({
                    "Date": tdate,
                    "Leg": "OUT" if ttype in OUT_LEG_TYPES else "IN",
                    "Type": ttype,
                    "Transfer?": "✓" if _is_transfer_leg(t.get("description") or "") else "",
                    "Scheme": r.scheme,
                    "Folio": f.folio,
                    "Amount": abs(float(t.get("amount") or 0)),
                    "Description": (t.get("description") or "")[:80],
                })
    with st.expander(f"🔍 Debug: STP candidate legs in the last 90 days ({len(leg_rows)})"):
        st.caption(
            "Every REDEMPTION/SWITCH_OUT (out-leg) and PURCHASE/PURCHASE_SIP/"
            "SWITCH_IN (in-leg) we see in the CAS. Only legs marked Transfer? ✓ "
            "(description says STP / Systematic Transfer / Switch) are paired — "
            "a plain buy or sell is ignored. Pairing then requires the dates "
            "within 3 days (legs often settle T+1/T+2), amount within ₹1, and "
            "differing ISINs. If an STP you expect isn't appearing, look for "
            "its two rows here and check the Transfer? mark and what differs."
        )
        if not leg_rows:
            st.info("No candidate legs in the last 90 days.")
            return
        df = pd.DataFrame(leg_rows).sort_values(["Date", "Leg"], ascending=[False, True])
        st.dataframe(df.style.format({"Amount": fmt_inr}),
                     use_container_width=True, hide_index=True)


def render_systematic(rows: list[SchemeRow]) -> None:
    # STPs run first so we know which PURCHASE_SIP transactions were actually
    # STP in-legs (they should not also be counted in the SIP list).
    stps, stp_in_tx_ids = detect_stps(rows)
    sips = detect_sips(rows, stp_in_tx_ids=stp_in_tx_ids)

    _render_summary(
        n_sips=len(sips),
        n_stps=len(stps),
        monthly_sip=sum(s.monthly_amount for s in sips),
        monthly_stp=sum(s.monthly_amount for s in stps),
    )
    st.divider()
    _render_sip_table(sips)
    st.divider()
    _render_stp_table(stps)
    st.divider()
    _render_debug_panel(rows)
