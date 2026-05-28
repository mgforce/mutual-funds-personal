"""
FastAPI auth gateway in front of the Streamlit dashboard.

Routes:
  - GET  /login, POST /login         — sign in
  - POST /logout                     — sign out
  - GET  /bootstrap, POST /bootstrap — first-launch admin signup (only when
                                        no admin exists yet; 404 otherwise)
  - GET  /invite/{token}, POST /...  — invitee creates their account
  - GET  /setup, POST /setup         — collect Gmail App Password + PDF
                                        password after signup, kick off CAS
  - GET  /static/*                   — auth-server's own CSS
  - everything else                  — proxied to Streamlit if authenticated,
                                        otherwise redirects to /login

The dashboard at Streamlit (port 8501) is never reachable directly — only
through this gateway. The gateway injects a signed payload header so the
dashboard knows who's logged in and has the data keys to decrypt CAS PDFs,
without ever seeing the user's KEK or password.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from analytics import auth, crypto, db, session_payload
from analytics.demo import DEMO_EMAIL, DEMO_PASSWORD
from auth_server import account, proxy, sessions

ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))

app = FastAPI(title="MF Portfolio — Auth Gateway")
app.mount("/_auth/static", StaticFiles(directory=str(ROOT / "static")), name="static")
app.include_router(account.router)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _render(name: str, request: Request, **ctx) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, name, ctx)


def _login_ctx(**extra) -> dict:
    """Add the demo-login hint when the demo account has actually been seeded —
    avoids advertising credentials that wouldn't work on a fresh clone."""
    if auth.user_exists(DEMO_EMAIL):
        extra.setdefault("demo_email", DEMO_EMAIL)
        extra.setdefault("demo_password", DEMO_PASSWORD)
    return extra


def _redirect(url: str) -> RedirectResponse:
    # 303 forces a GET regardless of the method that triggered the redirect.
    return RedirectResponse(url=url, status_code=303)


def _client_ip(request: Request) -> str:
    """Real visitor IP. Cloudflare Tunnel forwards it on CF-Connecting-IP;
    for direct origin access (local testing) fall back to the socket peer."""
    return (
        (request.headers.get("cf-connecting-ip") or "").strip()
        or (request.headers.get("x-forwarded-for", "").split(",")[0]).strip()
        or (request.client.host if request.client else "")
        or "unknown"
    )


@app.on_event("startup")
def _ensure_db() -> None:
    db.init_schema()


# ---------------------------------------------------------------------------
# Gating: which screen does the user belong on?
# ---------------------------------------------------------------------------

def _session_is_stale(sess: auth.Session) -> bool:
    """An in-memory Session can outlive the DB rows it holds keys for — e.g.
    an admin wipes & re-seeds the demo account out-of-band while a visitor's
    tab still carries the pre-seed cookie. The session's data_keys then
    decrypt nothing, and any later cred lookup raises InvalidToken (and 500).
    Detect that by probing one decryption per owned account; the caller can
    then bounce the user through /logout instead of into /setup."""
    for slug, key in sess.data_keys.items():
        acc = auth.get_cas_account(slug)
        if acc is None:
            return True  # account was deleted underneath us
        blob = acc.get("enc_pdf_password") or acc.get("enc_app_password")
        if not blob:
            continue  # nothing encrypted yet, can't probe
        try:
            crypto.decrypt_str(blob, key)
        except Exception:
            return True
    return False


def _next_screen(request: Request) -> str | None:
    """If the current request should be redirected somewhere else, return
    that URL. Used by the catch-all proxy and the root path to enforce the
    bootstrap → login → setup → dashboard flow."""
    if not auth.any_admin_exists():
        return "/bootstrap"

    sess = sessions.session_from_request(request)
    if sess is None:
        return "/login"

    if _session_is_stale(sess):
        return "/logout"

    pending = auth.needs_setup(sess)
    if pending:
        return "/setup"
    return None  # all good — let the request through


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    ip = _client_ip(request)
    if auth.is_ip_blocked(ip):
        return _render("blocked.html", request, ip=ip)
    redirect = _next_screen(request)
    if redirect and redirect != "/login":
        return _redirect(redirect)
    if sessions.session_from_request(request):
        return _redirect("/")
    return _render("login.html", request, **_login_ctx())


@app.post("/login")
def login_post(request: Request, email: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request)
    if auth.is_ip_blocked(ip):
        return _render("blocked.html", request, ip=ip)
    try:
        token, _ = auth.login(email, password)
    except ValueError as e:
        # Demo login is published — strangers typoing it shouldn't count
        # toward a permanent block. Real account misses do.
        normalized = (email or "").strip().lower()
        if normalized != DEMO_EMAIL:
            count = auth.record_failed_login(ip, normalized)
            if count >= auth.FAILED_LOGIN_THRESHOLD:
                auth.block_ip(
                    ip,
                    reason=f"exceeded {auth.FAILED_LOGIN_THRESHOLD} failed "
                           f"login attempts in {int(auth.FAILED_LOGIN_WINDOW.total_seconds()//3600)}h",
                )
                return _render("blocked.html", request, ip=ip)
        return _render("login.html", request, **_login_ctx(error=str(e), email=email))
    response = _redirect("/")
    sessions.attach(response, token)
    return response


@app.api_route("/logout", methods=["GET", "POST"])
def logout(request: Request):
    """Accept both verbs so Streamlit can navigate here via a plain link
    (no need for a hidden form to submit a POST). It's idempotent and only
    operates on the caller's own cookie, so the GET-vs-POST distinction
    doesn't carry the usual CSRF concerns for this app."""
    tok = sessions.token_from_request(request)
    auth.end_session(tok)
    response = _redirect("/login")
    sessions.detach(response)
    return response


@app.get("/bootstrap", response_class=HTMLResponse)
def bootstrap_get(request: Request):
    # Once admin is set up the bootstrap surface should not be enumerable
    # by random scanners. 404 (not redirect) so probes can't tell whether
    # this app is in the pre- or post-setup state.
    if auth.any_admin_exists():
        raise HTTPException(status_code=404)
    return _render("bootstrap.html", request, admin_email=auth.admin_email() or "")


@app.post("/bootstrap")
def bootstrap_post(
    request: Request,
    password: str = Form(...),
    confirm: str = Form(...),
):
    if auth.any_admin_exists():
        raise HTTPException(status_code=404)
    if password != confirm:
        return _render("bootstrap.html", request,
                       error="Passwords don't match.",
                       admin_email=auth.admin_email() or "")
    try:
        token, _ = auth.register_admin(auth.admin_email() or "", password)
    except ValueError as e:
        return _render("bootstrap.html", request,
                       error=str(e), admin_email=auth.admin_email() or "")
    response = _redirect("/setup")
    sessions.attach(response, token)
    return response


@app.get("/invite/{token}", response_class=HTMLResponse)
def invite_get(request: Request, token: str):
    invite = auth.get_invite(token)
    if not invite:
        return _render("invite.html", request, invalid=True)
    return _render("invite.html", request, invitee_email=invite["invitee_email"], token=token)


@app.post("/invite/{token}")
def invite_post(
    request: Request, token: str,
    password: str = Form(...), confirm: str = Form(...),
):
    invite = auth.get_invite(token)
    if not invite:
        return _render("invite.html", request, invalid=True)
    if password != confirm:
        return _render("invite.html", request,
                       invitee_email=invite["invitee_email"], token=token,
                       error="Passwords don't match.")
    try:
        new_token, _ = auth.accept_invite(token=token, password=password)
    except ValueError as e:
        return _render("invite.html", request,
                       invitee_email=invite["invitee_email"], token=token, error=str(e))
    response = _redirect("/setup")
    sessions.attach(response, new_token)
    return response


@app.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    sess = sessions.session_from_request(request)
    if sess is None:
        return _redirect("/login")
    slug = auth.needs_setup(sess)
    if not slug:
        return _redirect("/")
    creds = auth.get_account_creds(sess, slug)
    return _render("setup.html", request,
                   email=sess.user_email,
                   from_date=creds.get("from_date") or "2014-01-01")


@app.post("/setup")
def setup_post(
    request: Request,
    app_password: str = Form(...),
    pdf_password: str = Form(...),
    pdf_password_confirm: str = Form(...),
    from_date: str = Form(...),
):
    sess = sessions.session_from_request(request)
    if sess is None:
        return _redirect("/login")
    slug = auth.needs_setup(sess)
    if not slug:
        return _redirect("/")

    err = auth.validate_cams_pdf_password(pdf_password)
    if err:
        return _render("setup.html", request, error=err,
                       email=sess.user_email, from_date=from_date)
    if pdf_password != pdf_password_confirm:
        return _render("setup.html", request, error="PDF passwords don't match.",
                       email=sess.user_email, from_date=from_date)
    if not app_password.strip():
        return _render("setup.html", request, error="Gmail App Password is required.",
                       email=sess.user_email, from_date=from_date)

    auth.update_account_creds(
        sess, slug,
        app_password=app_password,
        pdf_password=pdf_password,
        from_date=from_date,
    )
    return _redirect("/")


# ---------------------------------------------------------------------------
# WebSocket + HTTP catch-all → proxy to Streamlit
# ---------------------------------------------------------------------------

def _payload_for(sess: auth.Session) -> str:
    return session_payload.build(sess.user_email, sess.is_admin, sess.data_keys)


@app.websocket("/{full_path:path}")
async def ws_catch_all(websocket: WebSocket, full_path: str):
    tok = sessions.token_from_cookie_value(websocket.cookies.get(sessions.COOKIE_NAME))
    sess = auth.get_session(tok) if tok else None
    if sess is None:
        await websocket.close(code=4401)
        return
    await proxy.proxy_websocket(websocket, _payload_for(sess))


@app.api_route("/{full_path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def http_catch_all(request: Request, full_path: str):
    redirect = _next_screen(request)
    if redirect:
        return _redirect(redirect)
    sess = sessions.session_from_request(request)
    if sess is None:  # belt-and-braces — _next_screen catches this already
        return _redirect("/login")
    return await proxy.proxy_http(request, _payload_for(sess))


# `/` is handled by the catch-all above — `full_path=""` matches it and is
# proxied through to Streamlit's own root. Listing it here as a separate
# route would shadow the catch-all and break the dashboard.
