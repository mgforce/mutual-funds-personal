"""
Local sqlite store for users, CAS accounts, access grants, invites.

Single file at data/app.db. Lives only on this machine — gitignored,
never synced, never uploaded. Cleared/restored by copying the file.

Schema is created on first connect; ALTER-style migrations are not
supported yet (the project is too young to need them).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email          TEXT PRIMARY KEY,
    password_hash  TEXT NOT NULL,
    kek_salt       BLOB NOT NULL,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cas_accounts (
    slug              TEXT PRIMARY KEY,
    email             TEXT NOT NULL,
    from_date         TEXT,
    enc_pdf_password  BLOB,
    enc_app_password  BLOB,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_access (
    user_email        TEXT NOT NULL,
    account_slug      TEXT NOT NULL,
    wrapped_data_key  BLOB NOT NULL,
    is_owner          INTEGER NOT NULL DEFAULT 0,
    granted_at        TEXT NOT NULL,
    PRIMARY KEY (user_email, account_slug),
    FOREIGN KEY (user_email)   REFERENCES users(email)        ON DELETE CASCADE,
    FOREIGN KEY (account_slug) REFERENCES cas_accounts(slug)  ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS invites (
    token              TEXT PRIMARY KEY,
    invitee_email      TEXT NOT NULL,
    invited_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    accepted_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_access_slug ON account_access(account_slug);
CREATE INDEX IF NOT EXISTS idx_invites_email ON invites(invitee_email);

-- Login throttling. Failed POSTs to /login record one row here per attempt;
-- once 5 fail-rows accumulate inside a 24-hour rolling window for a given
-- IP, that IP gets a permanent entry in ip_blocklist. Removal is manual via
-- scripts/unblock_ip.py — no auto-expiry, intentional for the public demo.
CREATE TABLE IF NOT EXISTS failed_login_attempts (
    ip            TEXT NOT NULL,
    email         TEXT,
    attempted_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failed_login_ip_time
    ON failed_login_attempts(ip, attempted_at);

CREATE TABLE IF NOT EXISTS ip_blocklist (
    ip          TEXT PRIMARY KEY,
    blocked_at  TEXT NOT NULL,
    reason      TEXT
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_schema() -> None:
    with connect() as c:
        c.executescript(_SCHEMA)


def fetchone(query: str, params: Iterable = ()) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute(query, tuple(params)).fetchone()


def fetchall(query: str, params: Iterable = ()) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(query, tuple(params)).fetchall()


def execute(query: str, params: Iterable = ()) -> None:
    with connect() as c:
        c.execute(query, tuple(params))
