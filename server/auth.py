"""SQLite-backed auth for the loci web UI.

Opaque session tokens in an HttpOnly cookie; only their SHA-256 is stored, so
a leaked database cannot be replayed as a login. Passwords go through
werkzeug's scrypt (Flask already depends on werkzeug).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = Path(
    os.environ.get("LOCI_DB")
    or (Path(os.environ.get("LOCI_STATE") or (Path.home() / ".local/state/loci")) / "loci.db")
)
SESSION_DAYS = 30
MAX_FAILURES = 10
LOCKOUT_MINUTES = 15

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    pw_hash    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS login_failures (
    id         INTEGER PRIMARY KEY,
    ip         TEXT NOT NULL,
    at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER,
    action  TEXT NOT NULL,
    detail  TEXT,
    ip      TEXT,
    at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failures_ip_at ON login_failures(ip, at);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _db() as conn:
        conn.executescript(SCHEMA)
    DB_PATH.chmod(0o600)


def has_users() -> bool:
    with _db() as conn:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_user(username: str, password: str) -> None:
    if not username or not password:
        raise ValueError("username and password are required")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    with _db() as conn:
        conn.execute(
            "INSERT INTO users (username, pw_hash, created_at) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), _iso(_now())),
        )


def delete_user(username: str) -> bool:
    with _db() as conn:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        return cur.rowcount > 0


def list_users() -> list[str]:
    with _db() as conn:
        return [r["username"] for r in conn.execute("SELECT username FROM users ORDER BY username")]


def locked_out(ip: str) -> bool:
    cutoff = _iso(_now() - timedelta(minutes=LOCKOUT_MINUTES))
    with _db() as conn:
        conn.execute("DELETE FROM login_failures WHERE at < ?", (cutoff,))
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM login_failures WHERE ip = ? AND at >= ?", (ip, cutoff)
        ).fetchone()["n"]
    return n >= MAX_FAILURES


def _record_failure(ip: str) -> None:
    with _db() as conn:
        conn.execute("INSERT INTO login_failures (ip, at) VALUES (?, ?)", (ip, _iso(_now())))


def verify_user(username: str, password: str, ip: str) -> Optional[int]:
    with _db() as conn:
        row = conn.execute(
            "SELECT id, pw_hash FROM users WHERE username = ?", (username,)
        ).fetchone()
    # Hash even when the user is unknown, so timing does not leak existence.
    expected = row["pw_hash"] if row else generate_password_hash("_no_such_user_")
    if row and check_password_hash(expected, password):
        with _db() as conn:
            conn.execute("DELETE FROM login_failures WHERE ip = ?", (ip,))
        return int(row["id"])
    check_password_hash(expected, password)
    _record_failure(ip)
    return None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int, user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with _db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(now),))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, user_agent)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                _hash_token(token),
                user_id,
                _iso(now),
                _iso(now + timedelta(days=SESSION_DAYS)),
                user_agent[:200],
            ),
        )
    return token


def validate_session(token: str) -> Optional[int]:
    if not token:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < _now():
        delete_session(token)
        return None
    return int(row["user_id"])


def delete_session(token: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def revoke_all_sessions() -> int:
    with _db() as conn:
        return conn.execute("DELETE FROM sessions").rowcount


def username_for(user_id: int) -> str:
    with _db() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["username"] if row else "?"


def log(user_id: Optional[int], action: str, detail: str = "", ip: str = "") -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO audit (user_id, action, detail, ip, at) VALUES (?, ?, ?, ?, ?)",
            (user_id, action, detail, ip, _iso(_now())),
        )


def recent_audit(limit: int = 50) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT a.at, a.action, a.detail, a.ip, COALESCE(u.username,'-') AS username"
            " FROM audit a LEFT JOIN users u ON u.id = a.user_id"
            " ORDER BY a.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# Kept for the legacy shared-token mode; constant-time, unlike ==.
def token_matches(supplied: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(supplied or "", expected)
