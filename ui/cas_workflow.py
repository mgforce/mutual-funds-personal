"""Sidebar buttons that drive the CAS refresh / inbox-process / re-parse flow.

The three buttons orchestrate a chain:
  Refresh CAS → submit form via Playwright → poll Gmail → write enc PDF
  Process inbox → fetch any newer CAS email
  Re-parse → drop cached parse so analytics re-runs with current rules
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import streamlit as st

from analytics.accounts import AccountContext
from analytics.state import load_state, update_state


def _render_status_summary(state: dict) -> None:
    """The ⏳ "waiting for CAMS email" reminder above the buttons."""
    last_req = state.get("last_request_at")
    last_fetched_at = state.get("last_fetched_at")
    if not last_req:
        st.caption("Click 'Refresh CAS' to fetch a fresh statement.")
        return
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


def _refresh_cas_button(ctx: AccountContext, slug: str, reset_caches: Callable[[], None]) -> None:
    # Local imports keep startup fast — playwright/imap aren't needed unless
    # the user actually clicks one of these.
    from ingest.cams_request import submit_via_playwright
    from ingest.gmail_fetch import fetch_latest_cas, peek_latest_uid

    try:
        baseline_uid = peek_latest_uid(ctx)
    except Exception:
        baseline_uid = None

    with st.spinner("Submitting CAS request to CAMS… (~30s)"):
        result = submit_via_playwright(ctx, dry_run=False)

    if not (result.get("ok") and result.get("submitted")):
        st.error(f"Submission failed: {result.get('error', 'unknown')}")
        return

    update_state(
        slug,
        last_request_at=datetime.now().isoformat(),
        request_baseline_uid=baseline_uid,
    )
    st.toast("✅ Submitted to CAMS — waiting for email.", icon="📨")

    poll_seconds = 600
    interval = 30
    deadline = time.time() + poll_seconds
    progress = st.progress(0.0, text="Waiting for CAMS email…")
    arrived_path: Path | None = None
    while time.time() < deadline:
        elapsed = poll_seconds - (deadline - time.time())
        mins, secs = divmod(int(elapsed), 60)
        progress.progress(
            min(elapsed / poll_seconds, 1.0),
            text=f"Waiting for CAMS email… {mins}m {secs:02d}s elapsed",
        )
        time.sleep(interval)
        try:
            new_uid = peek_latest_uid(ctx)
        except Exception:
            continue
        if new_uid and new_uid != baseline_uid:
            try:
                arrived_path = fetch_latest_cas(ctx, since_uid=baseline_uid)
            except Exception as e:
                st.error(f"Email arrived but fetch failed: {e}")
            break
    progress.empty()

    if arrived_path is not None:
        update_state(
            slug,
            last_fetched_pdf=str(arrived_path),
            last_fetched_at=datetime.now().isoformat(),
        )
        with st.spinner("Parsing the new statement (one-time, ~90s)…"):
            from analytics.portfolio import parse_cas
            parse_cas(ctx)
        reset_caches()
        st.success(f"✅ Got new CAS: {arrived_path.name}")
        st.rerun()
    else:
        st.warning(
            "⏳ Email hasn't arrived after 10 minutes. CAMS sometimes "
            "takes longer — click **Process inbox** once it lands."
        )


def _process_inbox_button(
    ctx: AccountContext,
    slug: str,
    state: dict,
    enc_pdf: Path | None,
    reset_caches: Callable[[], None],
) -> None:
    from ingest.gmail_fetch import NoNewCasError, fetch_latest_cas

    # Only fetch CAS emails newer than the last one we requested — guards
    # against picking up a stale email from before the current account
    # had its PDF password set (which would parse with wrong password).
    since_uid = state.get("request_baseline_uid")
    try:
        with st.spinner("Checking inbox…"):
            new_path = fetch_latest_cas(ctx, since_uid=since_uid)
        if enc_pdf is None or new_path.name != enc_pdf.name:
            update_state(
                slug,
                last_fetched_pdf=str(new_path),
                last_fetched_at=datetime.now().isoformat(),
            )
            with st.spinner("Parsing the new statement (one-time, ~90s)…"):
                from analytics.portfolio import parse_cas
                parse_cas(ctx)
            reset_caches()
            st.success(f"✅ Got new CAS: {new_path.name}")
            st.rerun()
        else:
            st.info("No newer CAS email yet — try again in a few minutes.")
    except NoNewCasError as e:
        st.warning(str(e))
    except Exception as e:
        st.error(f"Fetch failed: {e}")


def render_cas_workflow(
    ctx: AccountContext,
    slug: str,
    enc_pdf: Path | None,
    reset_caches: Callable[[], None],
) -> None:
    state = load_state(slug)

    st.markdown("### CAS workflow")
    _render_status_summary(state)

    st.caption("Request a fresh CAS from CAMS — they'll email the PDF in 5–30 min.")
    if st.button("🔄 Refresh CAS", use_container_width=True):
        _refresh_cas_button(ctx, slug, reset_caches)

    st.caption("Download the latest CAMS email and refresh the dashboard.")
    if st.button("📥 Process inbox", use_container_width=True):
        _process_inbox_button(ctx, slug, state, enc_pdf, reset_caches)

    st.caption("Re-fetch today's NAV from AMFI and recompute valuations (fast — skips PDF parser).")
    if st.button("📊 Re-parse current PDF with latest NAV", use_container_width=True):
        from analytics.nav import NAV_CACHE
        if NAV_CACHE.exists():
            NAV_CACHE.unlink()
        reset_caches()
        st.rerun()

    st.divider()
    # TODO: remove once classification/categorization rules are stable.
    st.caption("Force re-parse the current CAS (dev — after editing classification rules).")
    if st.button("🧹 Re-parse current PDF (dev)", use_container_width=True):
        p = ctx.parse_cache_path
        if p.exists():
            p.unlink()
        reset_caches()
        st.rerun()
