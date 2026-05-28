"""
User accounts, sessions, invites, account linking.

Flow:
  - Admin (configured via admin_email in config.yaml) is the only person who
    can mint invite tokens. Other people sign up by accepting an invite.
  - On login we derive the user's KEK from their password and unwrap the
    per-CAS-account data keys they have access to. Those data keys live in
    the in-memory Session for the rest of their browser session — never
    written back to disk.
  - "Link existing account" = Alice enters Bob's password, we verify it,
    derive Bob's KEK, unwrap Bob's data key, re-wrap it with Alice's KEK,
    and add an account_access row for Alice. Bob keeps his access.

Session state lives in this module's _SESSIONS dict, keyed by a random token
that the UI layer stashes in a browser cookie. Process restart logs everyone
out (intentional — keeps decrypted keys out of any persistence layer).
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from analytics import crypto, db
from analytics.crypto import InvalidToken

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
ACCOUNTS_DIR = ROOT / "data" / "accounts"

INVITE_TTL = timedelta(days=7)
SESSION_TTL = timedelta(days=7)

# Login throttling — IP-only, never per-email (per-email locks would let any
# stranger DoS the admin's own account). The window is rolling; once the
# threshold is hit inside that window, the IP lands in ip_blocklist
# permanently and only ``scripts/unblock_ip.py`` removes it.
FAILED_LOGIN_WINDOW = timedelta(hours=24)
FAILED_LOGIN_THRESHOLD = 5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.utcnow().isoformat()


def _utcnow() -> datetime:
    return datetime.utcnow()


def slugify(name: str) -> str:
    """Filesystem-safe slug for an account display name (typically email)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "account"


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def admin_email() -> str | None:
    """The single admin's email, configured in config.yaml."""
    if not CONFIG_PATH.exists():
        return None
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return _norm_email(cfg.get("admin_email") or "") or None


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@dataclass
class Session:
    user_email: str
    is_admin: bool
    kek: bytes
    data_keys: dict[str, bytes] = field(default_factory=dict)  # slug -> Fernet key
    expires_at: datetime = field(default_factory=lambda: _utcnow() + SESSION_TTL)

    def data_key(self, slug: str) -> bytes:
        if slug not in self.data_keys:
            raise PermissionError(f"No access to account: {slug}")
        return self.data_keys[slug]

    def slugs(self) -> list[str]:
        return list(self.data_keys.keys())


_SESSIONS: dict[str, Session] = {}


def get_session(token: str | None) -> Session | None:
    if not token:
        return None
    s = _SESSIONS.get(token)
    if s is None:
        return None
    if s.expires_at < _utcnow():
        _SESSIONS.pop(token, None)
        return None
    return s


def end_session(token: str | None) -> None:
    if token:
        _SESSIONS.pop(token, None)


def _start_session(user_email: str, is_admin: bool, kek: bytes) -> tuple[str, Session]:
    """Build a Session with all the user's data keys unwrapped, and stash it."""
    sess = Session(user_email=user_email, is_admin=is_admin, kek=kek)
    rows = db.fetchall(
        "SELECT account_slug, wrapped_data_key FROM account_access WHERE user_email = ?",
        (user_email,),
    )
    for row in rows:
        sess.data_keys[row["account_slug"]] = crypto.unwrap_key(row["wrapped_data_key"], kek)
    token = crypto.new_session_token()
    _SESSIONS[token] = sess
    return token, sess


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------

def user_exists(email: str) -> bool:
    return db.fetchone("SELECT 1 FROM users WHERE email = ?", (_norm_email(email),)) is not None


def any_admin_exists() -> bool:
    return db.fetchone("SELECT 1 FROM users WHERE is_admin = 1") is not None


def get_user(email: str) -> dict | None:
    row = db.fetchone("SELECT * FROM users WHERE email = ?", (_norm_email(email),))
    return dict(row) if row else None


def _create_user_with_account(
    *,
    email: str,
    password: str,
    app_password: str = "",
    pdf_password: str | None = None,
    from_date: str = "2014-01-01",
    is_admin: bool = False,
) -> tuple[str, Session]:
    """Create a user + their owned CAS account in one transaction-ish flow.
    Returns the new session (the user is implicitly logged in). The Gmail
    App Password can be omitted — it's collected on the post-signup setup
    screen, so the form the user fills first stays minimal."""
    email = _norm_email(email)
    if user_exists(email):
        raise ValueError(f"User already exists: {email}")

    slug = slugify(email)

    pwd_hash = crypto.hash_password(password)
    kek_salt = crypto.new_kek_salt()
    kek = crypto.derive_kek(password, kek_salt)
    data_key = crypto.new_data_key()
    wrapped = crypto.wrap_key(data_key, kek)
    # PDF / App passwords are collected on the post-signup setup screen — we
    # don't auto-default to the email anymore because the email can be longer
    # than CAMS allows (max 20 chars), which silently truncates the password.
    enc_pdf = crypto.encrypt_str(pdf_password, data_key) if pdf_password else None
    enc_app = crypto.encrypt_str((app_password or "").replace(" ", ""), data_key) if app_password else None

    with db.connect() as c:
        c.execute(
            "INSERT INTO users (email, password_hash, kek_salt, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, pwd_hash, kek_salt, 1 if is_admin else 0, _now()),
        )
        c.execute(
            "INSERT INTO cas_accounts (slug, email, from_date, enc_pdf_password, enc_app_password, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (slug, email, from_date, enc_pdf, enc_app, _now()),
        )
        c.execute(
            "INSERT INTO account_access (user_email, account_slug, wrapped_data_key, is_owner, granted_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (email, slug, wrapped, _now()),
        )

    (ACCOUNTS_DIR / slug).mkdir(parents=True, exist_ok=True)
    token, sess = _start_session(email, is_admin, kek)
    return token, sess


def register_admin(email: str, password: str) -> tuple[str, Session]:
    """First-launch bootstrap: create the admin user. Only callable when
    no admin exists yet and the email matches config.yaml's admin_email.
    Gmail App Password is collected later on the setup screen."""
    if any_admin_exists():
        raise ValueError("Admin already exists.")
    expected = admin_email()
    if not expected or _norm_email(email) != expected:
        raise ValueError(f"Admin email must match config.yaml admin_email ({expected}).")
    return _create_user_with_account(
        email=email,
        password=password,
        is_admin=True,
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(email: str, password: str) -> tuple[str, Session]:
    email = _norm_email(email)
    row = db.fetchone("SELECT * FROM users WHERE email = ?", (email,))
    if not row:
        raise ValueError("Invalid email or password.")
    if not crypto.verify_password(row["password_hash"], password):
        raise ValueError("Invalid email or password.")
    kek = crypto.derive_kek(password, row["kek_salt"])
    return _start_session(email, bool(row["is_admin"]), kek)


def change_password(session: Session, current_password: str, new_password: str) -> None:
    """Verify the current password, derive a new KEK, and re-wrap every data
    key the user has access to. Atomic: either everything updates or nothing
    does. The in-memory Session is mutated to carry the new KEK so the
    user's existing cookie keeps working — no forced logout."""
    from analytics.demo import DEMO_EMAIL
    if session.user_email == DEMO_EMAIL:
        raise ValueError("The demo account password is fixed and cannot be changed.")
    user = get_user(session.user_email)
    if not user:
        raise ValueError("User not found.")
    if not crypto.verify_password(user["password_hash"], current_password):
        raise ValueError("Current password is incorrect.")
    if not new_password or len(new_password) < 6:
        raise ValueError("New password must be at least 6 characters.")

    new_hash = crypto.hash_password(new_password)
    new_salt = crypto.new_kek_salt()
    new_kek = crypto.derive_kek(new_password, new_salt)

    rewrapped = {
        slug: crypto.wrap_key(data_key, new_kek)
        for slug, data_key in session.data_keys.items()
    }

    with db.connect() as c:
        c.execute(
            "UPDATE users SET password_hash = ?, kek_salt = ? WHERE email = ?",
            (new_hash, new_salt, session.user_email),
        )
        for slug, wrapped in rewrapped.items():
            c.execute(
                "UPDATE account_access SET wrapped_data_key = ? "
                "WHERE user_email = ? AND account_slug = ?",
                (wrapped, session.user_email, slug),
            )

    session.kek = new_kek


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

def create_invite(admin_user_email: str, invitee_email: str) -> str:
    """Admin creates a one-time signup token. Returns the raw token (give to invitee)."""
    admin_user_email = _norm_email(admin_user_email)
    invitee_email = _norm_email(invitee_email)
    user = get_user(admin_user_email)
    if not user or not user["is_admin"]:
        raise PermissionError("Only an admin can create invites.")
    if user_exists(invitee_email):
        raise ValueError(f"A user already exists for {invitee_email}.")

    token = crypto.new_invite_token()
    db.execute(
        "INSERT INTO invites (token, invitee_email, invited_by, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (token, invitee_email, admin_user_email, _now(), (_utcnow() + INVITE_TTL).isoformat()),
    )
    return token


def get_invite(token: str) -> dict | None:
    row = db.fetchone("SELECT * FROM invites WHERE token = ?", (token,))
    if not row:
        return None
    d = dict(row)
    if d["accepted_at"]:
        return None
    if datetime.fromisoformat(d["expires_at"]) < _utcnow():
        return None
    return d


def accept_invite(token: str, password: str) -> tuple[str, Session]:
    """Create the user and own-CAS-account from the invite. The Gmail App
    Password is collected on the post-signup setup screen, so this form
    stays minimal (just pick a password)."""
    invite = get_invite(token)
    if not invite:
        raise ValueError("Invite is invalid, expired, or already used.")
    new_token, sess = _create_user_with_account(
        email=invite["invitee_email"],
        password=password,
        is_admin=False,
    )
    db.execute("UPDATE invites SET accepted_at = ? WHERE token = ?", (_now(), token))
    return new_token, sess


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------

def link_existing_account(session: Session, target_email: str, target_password: str) -> str:
    """Grant ``session.user`` access to the CAS account owned by ``target_email``
    by entering target's login password. Returns the slug they now have access to.

    Verifies target's password → derives target's KEK → unwraps the data key →
    re-wraps it with the requester's KEK and persists an account_access row.
    Target keeps their own access; this is additive."""
    target_email = _norm_email(target_email)
    if target_email == session.user_email:
        raise ValueError("You already have access to your own account.")
    target = get_user(target_email)
    if not target:
        raise ValueError(
            f"No account exists for {target_email}. Ask the admin to invite "
            f"them first — they need to accept the invite and sign up before "
            f"you can link to their account."
        )
    if not crypto.verify_password(target["password_hash"], target_password):
        raise ValueError(f"Wrong password for {target_email}.")

    owned = db.fetchone(
        "SELECT account_slug, wrapped_data_key FROM account_access "
        "WHERE user_email = ? AND is_owner = 1",
        (target_email,),
    )
    if not owned:
        raise ValueError(
            f"{target_email} signed up but hasn't set up a CAS account yet. "
            f"Ask them to finish setup (Gmail App Password + PDF password) first."
        )

    target_kek = crypto.derive_kek(target_password, target["kek_salt"])
    try:
        data_key = crypto.unwrap_key(owned["wrapped_data_key"], target_kek)
    except InvalidToken:
        raise ValueError("Could not unwrap target account's key (password mismatch).")

    slug = owned["account_slug"]
    wrapped = crypto.wrap_key(data_key, session.kek)
    existing = db.fetchone(
        "SELECT 1 FROM account_access WHERE user_email = ? AND account_slug = ?",
        (session.user_email, slug),
    )
    if existing:
        db.execute(
            "UPDATE account_access SET wrapped_data_key = ?, granted_at = ? "
            "WHERE user_email = ? AND account_slug = ?",
            (wrapped, _now(), session.user_email, slug),
        )
    else:
        db.execute(
            "INSERT INTO account_access (user_email, account_slug, wrapped_data_key, is_owner, granted_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (session.user_email, slug, wrapped, _now()),
        )
    session.data_keys[slug] = data_key
    return slug


def unlink_account(session: Session, slug: str) -> None:
    """Remove the current user's access to a linked account. Refuses to unlink
    an account the user owns — use delete_my_account for that."""
    row = db.fetchone(
        "SELECT is_owner FROM account_access WHERE user_email = ? AND account_slug = ?",
        (session.user_email, slug),
    )
    if not row:
        return
    if row["is_owner"]:
        raise ValueError("You can't unlink your own account. Use 'Delete my account'.")
    db.execute(
        "DELETE FROM account_access WHERE user_email = ? AND account_slug = ?",
        (session.user_email, slug),
    )
    session.data_keys.pop(slug, None)


# ---------------------------------------------------------------------------
# Account info / credential access
# ---------------------------------------------------------------------------

def accounts_for_session(session: Session) -> list[tuple[str, str]]:
    """Return [(slug, email)] the user has access to, ordered by email."""
    if not session.data_keys:
        return []
    placeholders = ",".join("?" for _ in session.data_keys)
    rows = db.fetchall(
        f"SELECT slug, email FROM cas_accounts WHERE slug IN ({placeholders}) ORDER BY email",
        tuple(session.data_keys.keys()),
    )
    return [(r["slug"], r["email"]) for r in rows]


def get_cas_account(slug: str) -> dict | None:
    row = db.fetchone("SELECT * FROM cas_accounts WHERE slug = ?", (slug,))
    return dict(row) if row else None


def get_account_creds(session: Session, slug: str) -> dict:
    """Decrypt and return {email, from_date, pdf_password, app_password} for a slug."""
    acc = get_cas_account(slug)
    if not acc:
        raise KeyError(f"Unknown account: {slug}")
    key = session.data_key(slug)
    pdf_pw = crypto.decrypt_str(acc["enc_pdf_password"], key) if acc["enc_pdf_password"] else ""
    app_pw = crypto.decrypt_str(acc["enc_app_password"], key) if acc["enc_app_password"] else ""
    return {
        "email": acc["email"],
        "from_date": acc["from_date"] or "2014-01-01",
        "pdf_password": pdf_pw,
        "app_password": app_pw,
    }


def update_account_creds(
    session: Session,
    slug: str,
    *,
    pdf_password: str | None = None,
    app_password: str | None = None,
    from_date: str | None = None,
) -> None:
    """Update one or more credential fields. Empty strings are treated as
    "no change" — Streamlit password inputs sometimes round-trip empty even
    when a value was prefilled, and we don't want that to silently clobber
    a working password."""
    from analytics.demo import is_demo_slug
    if is_demo_slug(slug):
        raise ValueError("Credentials on the demo account are read-only.")
    key = session.data_key(slug)
    fields: list[str] = []
    params: list = []
    if pdf_password:
        fields.append("enc_pdf_password = ?")
        params.append(crypto.encrypt_str(pdf_password, key))
    if app_password:
        fields.append("enc_app_password = ?")
        params.append(crypto.encrypt_str(app_password.replace(" ", ""), key))
    if from_date is not None:
        fields.append("from_date = ?")
        params.append(from_date)
    if not fields:
        return
    params.append(slug)
    db.execute(f"UPDATE cas_accounts SET {', '.join(fields)} WHERE slug = ?", params)


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def _wipe_account_data(slug: str) -> None:
    target = ACCOUNTS_DIR / slug
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def delete_my_account(session: Session, password: str) -> None:
    """Confirm password, then wipe this user's data and the CAS account
    they own. Sessions for this user are invalidated."""
    user = get_user(session.user_email)
    if not user or not crypto.verify_password(user["password_hash"], password):
        raise ValueError("Incorrect password.")

    owned = db.fetchall(
        "SELECT account_slug FROM account_access WHERE user_email = ? AND is_owner = 1",
        (session.user_email,),
    )
    owned_slugs = [r["account_slug"] for r in owned]

    with db.connect() as c:
        c.execute("DELETE FROM users WHERE email = ?", (session.user_email,))
        for slug in owned_slugs:
            c.execute("DELETE FROM cas_accounts WHERE slug = ?", (slug,))

    for slug in owned_slugs:
        _wipe_account_data(slug)

    for tok, sess in list(_SESSIONS.items()):
        if sess.user_email == session.user_email:
            _SESSIONS.pop(tok, None)


# ---------------------------------------------------------------------------
# Login throttling — IP-only, manual unblock. See FAILED_LOGIN_* constants.
# ---------------------------------------------------------------------------

def _purge_old_failures(conn) -> None:
    """Drop fail-rows older than the rolling window so ``count_recent_failures``
    reflects 'in the last 24h' even though we never garbage-collect."""
    cutoff = (_utcnow() - FAILED_LOGIN_WINDOW).isoformat()
    conn.execute("DELETE FROM failed_login_attempts WHERE attempted_at < ?", (cutoff,))


def record_failed_login(ip: str, email: str) -> int:
    """Insert a failed-login row for this IP. Returns the resulting count of
    failures for the IP inside the rolling window so the caller can decide
    whether the threshold has been crossed and a permanent block should fire."""
    if not ip:
        return 0
    with db.connect() as c:
        _purge_old_failures(c)
        c.execute(
            "INSERT INTO failed_login_attempts (ip, email, attempted_at) VALUES (?, ?, ?)",
            (ip, email or "", _now()),
        )
        row = c.execute(
            "SELECT COUNT(*) AS n FROM failed_login_attempts WHERE ip = ?",
            (ip,),
        ).fetchone()
    return int(row["n"])


def is_ip_blocked(ip: str) -> bool:
    if not ip:
        return False
    return db.fetchone("SELECT 1 FROM ip_blocklist WHERE ip = ?", (ip,)) is not None


def block_ip(ip: str, reason: str = "") -> None:
    """Permanent block. ``INSERT OR IGNORE`` keeps re-calls idempotent."""
    if not ip:
        return
    db.execute(
        "INSERT OR IGNORE INTO ip_blocklist (ip, blocked_at, reason) VALUES (?, ?, ?)",
        (ip, _now(), reason or ""),
    )


def unblock_ip(ip: str) -> int:
    """Remove an IP from the blocklist AND clear its failed-login history so
    it starts fresh. Returns 1 if a blocklist row was deleted, 0 otherwise."""
    if not ip:
        return 0
    with db.connect() as c:
        cur = c.execute("DELETE FROM ip_blocklist WHERE ip = ?", (ip,))
        removed = cur.rowcount or 0
        c.execute("DELETE FROM failed_login_attempts WHERE ip = ?", (ip,))
    return removed


def list_blocked_ips() -> list[dict]:
    rows = db.fetchall(
        "SELECT ip, blocked_at, reason FROM ip_blocklist ORDER BY blocked_at DESC"
    )
    return [dict(r) for r in rows]


def validate_cams_pdf_password(pw: str) -> str | None:
    """CAMS form constraints. Returns an error string, or None if valid."""
    if not pw:
        return "PDF password is required."
    if len(pw) < 6 or len(pw) > 15:
        return "PDF password must be 6–15 characters."
    if not any(c.isupper() for c in pw):
        return "PDF password must contain at least one uppercase letter."
    if not any(c.islower() for c in pw):
        return "PDF password must contain at least one lowercase letter."
    if not any(c.isdigit() for c in pw):
        return "PDF password must contain at least one digit."
    return None


def needs_setup(session: Session) -> str | None:
    """Return the slug of an owned CAS account that's still missing one of
    the required setup-screen fields (Gmail App Password or CAS PDF password).
    None when every owned account is fully configured."""
    rows = db.fetchall(
        "SELECT ca.slug, ca.enc_app_password, ca.enc_pdf_password FROM cas_accounts ca "
        "JOIN account_access aa ON aa.account_slug = ca.slug "
        "WHERE aa.user_email = ? AND aa.is_owner = 1",
        (session.user_email,),
    )
    for row in rows:
        for col in ("enc_app_password", "enc_pdf_password"):
            blob = row[col]
            if not blob:
                return row["slug"]
            try:
                value = crypto.decrypt_str(blob, session.data_key(row["slug"]))
            except Exception:
                return row["slug"]
            if not value.strip():
                return row["slug"]
    return None


__all__ = [
    "FAILED_LOGIN_THRESHOLD",
    "FAILED_LOGIN_WINDOW",
    "Session",
    "accept_invite",
    "accounts_for_session",
    "admin_email",
    "any_admin_exists",
    "block_ip",
    "change_password",
    "create_invite",
    "delete_my_account",
    "end_session",
    "get_account_creds",
    "get_cas_account",
    "get_invite",
    "get_session",
    "get_user",
    "is_ip_blocked",
    "link_existing_account",
    "list_blocked_ips",
    "login",
    "needs_setup",
    "record_failed_login",
    "register_admin",
    "slugify",
    "unblock_ip",
    "unlink_account",
    "update_account_creds",
    "user_exists",
    "validate_cams_pdf_password",
]
