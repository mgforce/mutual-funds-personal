"""
Account-mutation endpoints, plus the change-password page.

The sidebar in Streamlit hosts the settings UI as a dropdown — small forms
that POST straight here. After we mutate state we 303-redirect back to
``/`` with ``?flash=<key>`` so the dashboard shows a toast.

The one exception is **change login password**: it gets its own page
because it needs old + new + confirm fields and a chunkier explainer. The
sidebar has a link to ``/account/password`` rather than a form.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from analytics import auth
from auth_server import sessions

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def _back_to_dashboard(flash: Optional[str] = None) -> RedirectResponse:
    url = f"/?flash={flash}" if flash else "/"
    return RedirectResponse(url=url, status_code=303)


def _require_session(request: Request) -> Optional[auth.Session]:
    return sessions.session_from_request(request)


# ---------------------------------------------------------------------------
# Change login password — dedicated page
# ---------------------------------------------------------------------------

@router.get("/account/password", response_class=HTMLResponse)
def change_password_page(request: Request, flash: Optional[str] = None):
    sess = _require_session(request)
    if sess is None:
        return RedirectResponse("/login", status_code=303)
    return TEMPLATES.TemplateResponse(request, "change_password.html", {
        "session": sess,
        "flash": flash,
    })


@router.post("/account/password")
def change_password_post(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm: str = Form(...),
):
    sess = _require_session(request)
    if sess is None:
        return RedirectResponse("/login", status_code=303)
    if new_password != confirm:
        return RedirectResponse("/account/password?flash=mismatch", status_code=303)
    try:
        auth.change_password(sess, current_password, new_password)
    except ValueError as e:
        return RedirectResponse(f"/account/password?flash=err-{e}", status_code=303)
    return _back_to_dashboard("password-changed")


# ---------------------------------------------------------------------------
# Sidebar-driven actions
# ---------------------------------------------------------------------------

@router.post("/account/{slug}/app-password")
def set_app_password(request: Request, slug: str, app_password: str = Form(...)):
    sess = _require_session(request)
    if sess is None or slug not in sess.data_keys:
        return _back_to_dashboard("err-no-access")
    if not app_password.strip():
        return _back_to_dashboard("err-empty-password")
    try:
        auth.update_account_creds(sess, slug, app_password=app_password.strip())
    except ValueError as e:
        return _back_to_dashboard(f"err-{e}")
    return _back_to_dashboard("app-pw-saved")


@router.post("/account/{slug}/pdf-password")
def set_pdf_password(request: Request, slug: str, pdf_password: str = Form(...)):
    sess = _require_session(request)
    if sess is None or slug not in sess.data_keys:
        return _back_to_dashboard("err-no-access")
    err = auth.validate_cams_pdf_password(pdf_password)
    if err:
        return _back_to_dashboard(f"err-{err}")
    try:
        auth.update_account_creds(sess, slug, pdf_password=pdf_password.strip())
    except ValueError as e:
        return _back_to_dashboard(f"err-{e}")
    return _back_to_dashboard("pdf-pw-saved")


@router.post("/account/link")
def link_account_post(
    request: Request,
    target_email: str = Form(...),
    target_password: str = Form(...),
):
    """Submitted by the inline sidebar form. Streamlit reads the redirect
    Location header to decide success/error without ever following it."""
    sess = _require_session(request)
    if sess is None:
        return RedirectResponse("/login", status_code=303)
    try:
        auth.link_existing_account(sess, target_email, target_password)
    except (ValueError, PermissionError) as e:
        return _back_to_dashboard(f"err-{e}")
    return _back_to_dashboard("linked")


@router.api_route("/account/{slug}/unlink", methods=["GET", "POST"])
def unlink(request: Request, slug: str):
    sess = _require_session(request)
    if sess is None:
        return RedirectResponse("/login", status_code=303)
    try:
        auth.unlink_account(sess, slug)
    except ValueError as e:
        return _back_to_dashboard(f"err-{e}")
    return _back_to_dashboard("unlinked")


@router.get("/account/delete", response_class=HTMLResponse)
def delete_account_page(request: Request, flash: Optional[str] = None):
    sess = _require_session(request)
    if sess is None:
        return RedirectResponse("/login", status_code=303)
    return TEMPLATES.TemplateResponse(request, "delete_account.html", {
        "session": sess,
        "flash": flash,
    })


@router.post("/account/delete")
def delete_my_account(request: Request, password: str = Form(...)):
    sess = _require_session(request)
    if sess is None:
        return RedirectResponse("/login", status_code=303)
    try:
        auth.delete_my_account(sess, password)
    except ValueError as e:
        # Stay on the delete page so the user can retry / read the error.
        return RedirectResponse(f"/account/delete?flash=err-{e}", status_code=303)
    response = RedirectResponse("/login?flash=account-deleted", status_code=303)
    sessions.detach(response)
    return response


@router.post("/account/invite")
def invite(request: Request, invitee_email: str = Form(...)):
    sess = _require_session(request)
    if sess is None or not sess.is_admin:
        return _back_to_dashboard("err-not-admin")
    try:
        token = auth.create_invite(sess.user_email, invitee_email)
    except (ValueError, PermissionError) as e:
        return _back_to_dashboard(f"err-{e}")
    return _back_to_dashboard(f"invited-{token}")
