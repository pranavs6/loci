"""Saved locations, owned per user, with soft delete.

Rows are never removed: `deleted_at` is stamped instead, so a location that
still appears in the audit log can always be resolved back to a name.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from auth import connection

# ~11 m. Tight enough that two saved places stay distinct, loose enough that a
# coordinate round-tripped through the map still matches what was saved.
MATCH_DP = 4

DEFAULTS = [
    ("London", 51.5007, -0.1246),
    ("Bangalore", 12.9716, 77.5946),
    ("Mumbai", 19.0760, 72.8777),
    ("Delhi", 28.6139, 77.2090),
    ("New York", 40.7580, -73.9855),
    ("San Francisco", 37.7749, -122.4194),
    ("Tokyo", 35.6762, 139.6503),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_locations_name
    ON locations(user_id, name) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_locations_user ON locations(user_id, deleted_at);
"""


class StoreError(Exception):
    """Something the user should see, rather than a 500."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(SCHEMA)


def _clean(name: str, lat, lon) -> tuple[str, float, float]:
    name = (name or "").strip()
    if not name:
        raise StoreError("name is required")
    if len(name) > 60:
        raise StoreError("name must be 60 characters or fewer")
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        raise StoreError("latitude and longitude must be numbers") from None
    if not -90 <= lat_f <= 90:
        raise StoreError("latitude must be between -90 and 90")
    if not -180 <= lon_f <= 180:
        raise StoreError("longitude must be between -180 and 180")
    return name, lat_f, lon_f


def list_locations(user_id: int, include_deleted: bool = False) -> list[dict]:
    sql = (
        "SELECT id, name, lat, lon, created_at, updated_at, deleted_at"
        " FROM locations WHERE user_id = ?"
    )
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    sql += " ORDER BY deleted_at IS NOT NULL, name COLLATE NOCASE"
    with connection() as conn:
        return [dict(r) for r in conn.execute(sql, (user_id,)).fetchall()]


def get_location(user_id: int, loc_id: int) -> Optional[dict]:
    with connection() as conn:
        row = conn.execute(
            "SELECT id, name, lat, lon, deleted_at FROM locations"
            " WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (loc_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def find_by_coords(user_id: int, lat: float, lon: float) -> Optional[dict]:
    with connection() as conn:
        row = conn.execute(
            "SELECT id, name, lat, lon FROM locations"
            " WHERE user_id = ? AND deleted_at IS NULL"
            " AND ROUND(lat, ?) = ROUND(?, ?) AND ROUND(lon, ?) = ROUND(?, ?)",
            (user_id, MATCH_DP, lat, MATCH_DP, MATCH_DP, lon, MATCH_DP),
        ).fetchone()
    return dict(row) if row else None


def create_location(user_id: int, name: str, lat, lon) -> dict:
    name, lat_f, lon_f = _clean(name, lat, lon)
    now = _now()
    with connection() as conn:
        existing = conn.execute(
            "SELECT id FROM locations WHERE user_id = ? AND name = ? AND deleted_at IS NOT NULL",
            (user_id, name),
        ).fetchone()
        if existing:  # reuse the soft-deleted row so the name stays unique
            conn.execute(
                "UPDATE locations SET lat = ?, lon = ?, updated_at = ?, deleted_at = NULL"
                " WHERE id = ?",
                (lat_f, lon_f, now, existing["id"]),
            )
            loc_id = int(existing["id"])
        else:
            try:
                cur = conn.execute(
                    "INSERT INTO locations (user_id, name, lat, lon, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, name, lat_f, lon_f, now, now),
                )
            except sqlite3.IntegrityError:
                raise StoreError(f"a location named {name!r} already exists") from None
            loc_id = int(cur.lastrowid)
    return {"id": loc_id, "name": name, "lat": lat_f, "lon": lon_f}


def update_location(user_id: int, loc_id: int, name: str, lat, lon) -> dict:
    name, lat_f, lon_f = _clean(name, lat, lon)
    with connection() as conn:
        try:
            cur = conn.execute(
                "UPDATE locations SET name = ?, lat = ?, lon = ?, updated_at = ?"
                " WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
                (name, lat_f, lon_f, _now(), loc_id, user_id),
            )
        except sqlite3.IntegrityError:
            raise StoreError(f"a location named {name!r} already exists") from None
        if cur.rowcount == 0:
            raise StoreError("no such location")
    return {"id": loc_id, "name": name, "lat": lat_f, "lon": lon_f}


def soft_delete(user_id: int, loc_id: int) -> bool:
    with connection() as conn:
        cur = conn.execute(
            "UPDATE locations SET deleted_at = ?, updated_at = ?"
            " WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (_now(), _now(), loc_id, user_id),
        )
        return cur.rowcount > 0


def restore(user_id: int, loc_id: int) -> bool:
    with connection() as conn:
        row = conn.execute(
            "SELECT name FROM locations WHERE id = ? AND user_id = ? AND deleted_at IS NOT NULL",
            (loc_id, user_id),
        ).fetchone()
        if row is None:
            return False
        clash = conn.execute(
            "SELECT 1 FROM locations WHERE user_id = ? AND name = ? AND deleted_at IS NULL",
            (user_id, row["name"]),
        ).fetchone()
        if clash:
            raise StoreError(f"a location named {row['name']!r} already exists")
        conn.execute(
            "UPDATE locations SET deleted_at = NULL, updated_at = ? WHERE id = ?",
            (_now(), loc_id),
        )
        return True


def seed_defaults(user_id: int) -> int:
    with connection() as conn:
        if conn.execute(
            "SELECT 1 FROM locations WHERE user_id = ? LIMIT 1", (user_id,)
        ).fetchone():
            return 0
        now = _now()
        conn.executemany(
            "INSERT INTO locations (user_id, name, lat, lon, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(user_id, n, la, lo, now, now) for n, la, lo in DEFAULTS],
        )
    return len(DEFAULTS)
