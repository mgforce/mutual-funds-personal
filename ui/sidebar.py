"""Sidebar: account picker, admin invite, settings dropdown.

Settings UI is a tight expander. Each cred-update collapses into a button
by default — clicking it reveals the input + Save / Cancel. Keeps the menu
visually quiet when the user is just glancing at it.

Routing recap:
  - Gmail / PDF update → ``auth.update_account_creds`` in-process.
  - Change login password → its own gateway page (needs KEK).
  - Unlink → gateway endpoint so auth_server prunes its session too.
  - Logout → gateway ``/logout``.
  - Delete account → its own gateway page.
"""
from __future__ import annotations

from typing import Callable

import streamlit as st

import httpx

from analytics import auth
from analytics.demo import DEMO_EMAIL, is_demo_slug
from ui.auth_glue import active_slug


# The Streamlit dashboard always sits behind the FastAPI gateway on the same
# host; the link form just relays to the gateway endpoint over loopback so
# auth_server (which holds the KEK) can do the actual key-unwrap work.
_GATEWAY_URL = "http://127.0.0.1:8000"


def render_account_picker(session: auth.Session, on_account_change: Callable[[], None]) -> str | None:
    accounts = auth.accounts_for_session(session)
    if not accounts:
        st.info("No CAS accounts yet.")
        return None

    options = [slug for slug, _ in accounts]
    label_for = {slug: email for slug, email in accounts}
    active = active_slug(session)
    idx = options.index(active) if active in options else 0

    chosen = st.selectbox(
        "Account",
        options=options,
        index=idx,
        format_func=lambda s: label_for.get(s, s),
        key="acc_picker",
    )
    _render_link_expander(disabled=session.user_email == DEMO_EMAIL)

    if chosen != active:
        st.session_state["_active_slug"] = chosen
        st.query_params["account"] = chosen
        if "scheme" in st.query_params:
            del st.query_params["scheme"]
        on_account_change()
        st.rerun()

    if session.is_admin:
        _render_invite_expander(session)

    own_slug = auth.slugify(session.user_email)
    _render_settings_expander(session, chosen, is_owner=chosen == own_slug)
    return chosen


def _render_link_expander(*, disabled: bool = False) -> None:
    """Inline form: enter another user's email + login password and submit
    without leaving the dashboard. Streamlit calls the gateway over loopback,
    forwarding the user's auth cookie so the gateway resolves the right
    Session (and its KEK) for the link operation.

    ``disabled`` greys out the form (used for the demo account) without
    removing the expander, so the sidebar layout matches a real account."""
    # Auto-open the expander while we're showing the success state so the
    # user sees the linked-email confirmation immediately on rerun.
    just_linked = st.session_state.get("_just_linked")
    with st.expander("➕ Link another account", expanded=bool(just_linked)):
        if disabled:
            with st.form("link_account_form_disabled", clear_on_submit=False):
                st.text_input("Account email", value="", disabled=True,
                              key="_link_email_disabled")
                st.text_input("Account login password", type="password",
                              value="", disabled=True,
                              key="_link_pw_disabled")
                st.form_submit_button(
                    "Link account", type="primary", use_container_width=True,
                    disabled=True, help="Disabled on the demo account.",
                )
            return
        if just_linked:
            st.success(f"Linked **{just_linked}**.")
            st.caption(
                "Refresh the dashboard to see them in the account dropdown — "
                "the session needs a fresh handshake to pick up the new account."
            )
            st.link_button("🔄 Refresh dashboard", "/", type="primary",
                           use_container_width=True)
            if st.button("Link another account", use_container_width=True,
                         key="link_another"):
                del st.session_state["_just_linked"]
                st.rerun()
            return

        # Streamlit shows a "Press Enter to submit form" hint under every
        # text_input inside a form. Hide it — clutters the small sidebar.
        st.markdown(
            "<style>[data-testid='InputInstructions']{display:none!important;}</style>",
            unsafe_allow_html=True,
        )
        with st.form("link_account_form", clear_on_submit=True):
            email = st.text_input("Account email", autocomplete="off")
            password = st.text_input(
                "Account login password", type="password", autocomplete="off",
                help="Used once to unlock their CAS data; never stored.",
            )
            submit = st.form_submit_button("Link account", type="primary",
                                            use_container_width=True)

        if not submit:
            return
        if not email.strip() or not password:
            st.error("Both email and password are required.")
            return

        # Forward cookies as a raw Cookie header — bypasses any httpx cookie-jar
        # domain matching that would drop a cookie set for a different host
        # (e.g. when the dashboard is accessed via LAN IP but Streamlit calls
        # the gateway over loopback).
        cookies = dict(st.context.cookies or {})
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

        try:
            resp = httpx.post(
                f"{_GATEWAY_URL}/account/link",
                data={"target_email": email.strip(), "target_password": password},
                headers={"Cookie": cookie_header} if cookie_header else {},
                follow_redirects=False,
                timeout=10.0,
            )
        except Exception as e:
            st.error(f"Couldn't reach the auth gateway: {e}")
            return

        # Gateway always 303-redirects: success → /?flash=linked,
        # validation failure → /?flash=err-<msg>, no session → /login.
        from urllib.parse import unquote
        location = resp.headers.get("location", "")
        if "flash=linked" in location:
            # Stash the linked email so the next rerun renders a clear
            # success panel + refresh prompt instead of a cleared form.
            st.session_state["_just_linked"] = email.strip()
            st.rerun()
        elif "flash=err-" in location:
            err = unquote(location.split("flash=err-", 1)[1])
            st.error(err)
        elif location.startswith("/login"):
            cookie_names = list(cookies.keys()) or "(none)"
            st.error(
                f"Gateway didn't recognise your session (cookies forwarded: "
                f"{cookie_names}). Reload the dashboard page and log in again."
            )
        else:
            st.error(f"Unexpected gateway response (HTTP {resp.status_code}, "
                     f"location={location!r}).")


def _render_invite_expander(session: auth.Session) -> None:
    with st.expander("👥 Invite a user"):
        st.caption("Generates a one-time invite link. Share it out-of-band — "
                   "they pick their own password on first visit. Expires in 7 days.")
        with st.form("invite_form", clear_on_submit=True):
            invitee = st.text_input("Email to invite")
            submit = st.form_submit_button("Generate invite", type="primary", use_container_width=True)
        if submit and invitee.strip():
            try:
                token = auth.create_invite(session.user_email, invitee.strip())
            except (ValueError, PermissionError) as e:
                st.error(str(e))
                return
            st.success("Invite link:")
            st.code(f"/invite/{token}", language=None)


def _render_settings_expander(session: auth.Session, slug: str, *, is_owner: bool) -> None:
    with st.expander("⚙️ Settings"):
        st.caption(f"Logged in as **{session.user_email}**"
                   + (" (admin)" if session.is_admin else ""))

        if session.user_email == DEMO_EMAIL:
            st.markdown(
                "<span style='opacity:0.5;cursor:not-allowed;' "
                "title='Disabled on the demo account'>"
                "🔒 Change login password</span>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<a href='/account/password' target='_self'>🔒 Change login password</a>",
                unsafe_allow_html=True,
            )
        st.divider()

        if is_demo_slug(slug):
            st.button(
                "✉️ Set new Gmail App Password",
                key=f"_btn_gmail_pw_{slug}_disabled",
                disabled=True,
                use_container_width=True,
                help="Disabled on the demo account.",
            )
            st.button(
                "🔑 Set new CAS PDF password",
                key=f"_btn_pdf_pw_{slug}_disabled",
                disabled=True,
                use_container_width=True,
                help="Disabled on the demo account.",
            )
        elif is_owner:
            _render_inline_password_setter(
                session, slug,
                form_key="gmail_pw",
                label="✉️ Set new Gmail App Password",
                input_label="Gmail App Password",
                input_help=None,
                cred_field="app_password",
                success_msg="Gmail App Password saved",
            )
            _render_inline_password_setter(
                session, slug,
                form_key="pdf_pw",
                label="🔑 Set new CAS PDF password",
                input_label="CAS PDF password",
                input_help="6–15 chars: one upper, one lower, one digit.",
                cred_field="pdf_password",
                success_msg="CAS PDF password saved",
                validator=auth.validate_cams_pdf_password,
            )
        else:
            st.caption("This account belongs to another user. Their creds "
                       "aren't editable from here.")
            st.link_button("🔗 Unlink this account", f"/account/{slug}/unlink",
                           use_container_width=True)

        st.divider()
        st.link_button("🚪 Log out", "/logout", type="primary", use_container_width=True)

        if session.user_email == DEMO_EMAIL:
            st.markdown(
                "<span style='color:#f87171;font-size:0.9em;"
                "opacity:0.5;cursor:not-allowed;' "
                "title='Disabled on the demo account'>"
                "🗑️ Delete my account</span>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<a href='/account/delete' target='_self' "
                "style='color:#f87171;font-size:0.9em;'>🗑️ Delete my account</a>",
                unsafe_allow_html=True,
            )


def _render_inline_password_setter(
    session: auth.Session,
    slug: str,
    *,
    form_key: str,
    label: str,
    input_label: str,
    input_help: str | None,
    cred_field: str,
    success_msg: str,
    validator: Callable[[str], str | None] | None = None,
) -> None:
    """Disclosure pattern: a button by default; click reveals input + Save / Cancel.
    State lives in session_state so the form stays open across reruns until
    explicitly closed."""
    open_key = f"_open_{form_key}_{slug}"

    if not st.session_state.get(open_key):
        if st.button(label, key=f"_btn_{form_key}_{slug}", use_container_width=True):
            st.session_state[open_key] = True
            st.rerun()
        return

    with st.form(f"_form_{form_key}_{slug}", clear_on_submit=True):
        new_value = st.text_input(input_label, type="password", help=input_help)
        cols = st.columns([1, 1])
        with cols[0]:
            save = st.form_submit_button("Save", type="primary", use_container_width=True)
        with cols[1]:
            cancel = st.form_submit_button("Cancel", use_container_width=True)

    if cancel:
        st.session_state[open_key] = False
        st.rerun()
    if save:
        if validator:
            err = validator(new_value)
            if err:
                st.error(err)
                return
        elif not new_value.strip():
            st.error(f"{input_label} is required.")
            return
        auth.update_account_creds(session, slug, **{cred_field: new_value.strip()})
        st.toast(success_msg, icon="✅")
        st.session_state[open_key] = False
        st.rerun()
