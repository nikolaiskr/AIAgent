from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from projects import list_projects, load_project_meta
from settings import DB_PATH, Settings

FOREVER = date(9999, 12, 31)


class PaymentRequired(RuntimeError):
    """Лимит бесплатных работ исчерпан — нужна оплата."""

    def __init__(self, used: int, limit: int) -> None:
        super().__init__(f"Бесплатный лимит исчерпан: создано {used} из {limit} работ.")
        self.used = used
        self.limit = limit


@dataclass(slots=True)
class UserRow:
    user_id: int
    username: str | None
    first_name: str | None
    paid_until: str | None
    notes: str | None
    created_at: str
    updated_at: str
    projects: int = 0


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            paid_until TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            user_id INTEGER,
            project TEXT,
            message TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)")
    conn.commit()
    return conn


def touch_user(user_id: int, username: str | None = None, first_name: str | None = None) -> bool:
    """Регистрирует пользователя. Возвращает True, если пользователь новый."""
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        row = conn.execute("SELECT user_id FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        is_new = row is None
        conn.execute(
            """
            INSERT INTO users(user_id, username, first_name, paid_until, notes, created_at, updated_at)
            VALUES(?,?,?,?,NULL,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, users.username),
                first_name = COALESCE(excluded.first_name, users.first_name),
                updated_at = excluded.updated_at
            """,
            (int(user_id), username, first_name, None, now, now),
        )
        conn.commit()
    return is_new


def get_user(user_id: int) -> UserRow | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id, username, first_name, paid_until, notes, created_at, updated_at FROM users WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    if not row:
        return None
    return UserRow(*row)


def is_paid(user_id: int) -> bool:
    user = get_user(user_id)
    if user is None or not user.paid_until:
        return False
    try:
        until = date.fromisoformat(user.paid_until[:10])
    except ValueError:
        return False
    return until >= date.today()


def grant_paid(user_id: int, days: int | None = None, notes: str | None = None) -> str:
    """days=None → безлимит навсегда; иначе оплаченный период в днях от сегодня."""
    until = FOREVER if days is None else date.today() + timedelta(days=max(1, int(days)))
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users(user_id, username, first_name, paid_until, notes, created_at, updated_at)
            VALUES(?,?,NULL,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                paid_until = excluded.paid_until,
                notes = COALESCE(excluded.notes, users.notes),
                updated_at = excluded.updated_at
            """,
            (int(user_id), None, until.isoformat(), notes, now, now),
        )
        conn.commit()
    return until.isoformat()


def revoke_paid(user_id: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET paid_until=NULL, updated_at=? WHERE user_id=?",
            (now, int(user_id)),
        )
        conn.commit()


def user_project_count(settings: Settings, user_id: int) -> int:
    """Число работ, закреплённых за пользователем (owner_user_id в project.json)."""
    count = 0
    for project in list_projects(settings):
        try:
            meta = load_project_meta(settings, project.name)
        except Exception:
            continue
        if meta.get("owner_user_id") in (None, ""):
            continue
        try:
            if int(meta["owner_user_id"]) == int(user_id):
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def quota_status(settings: Settings, user_id: int) -> dict[str, Any]:
    """Статус квоты пользователя для Mini App и гейта оплаты."""
    user = get_user(user_id)
    paid = is_paid(user_id)
    used = user_project_count(settings, user_id)
    limit = settings.free_projects
    return {
        "paid": paid,
        "paid_until": user.paid_until if user else None,
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used) if not paid else None,
        "projects": used,
    }


def check_project_quota(settings: Settings, user_id: int) -> None:
    """Бросает PaymentRequired, если пользователь исчерпал бесплатный лимит и не оплачен."""
    if is_paid(user_id):
        return
    used = user_project_count(settings, user_id)
    if used >= settings.free_projects:
        raise PaymentRequired(used, settings.free_projects)


def log_event(kind: str, message: str, user_id: int | None = None, project: str | None = None) -> None:
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO events(ts, kind, user_id, project, message) VALUES(?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), kind[:40], user_id, project, message[:2000]),
            )
            conn.commit()
    except sqlite3.Error:
        pass


def recent_events(limit: int = 100) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, kind, user_id, project, message FROM events ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [
        {"ts": r[0], "kind": r[1], "user_id": r[2], "project": r[3], "message": r[4]}
        for r in rows
    ]


def list_users(settings: Settings) -> list[UserRow]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT user_id, username, first_name, paid_until, notes, created_at, updated_at FROM users ORDER BY created_at DESC"
        ).fetchall()
    users = [UserRow(*row) for row in rows]
    counts: dict[int, int] = {}
    for project in list_projects(settings):
        try:
            meta = load_project_meta(settings, project.name)
        except Exception:
            continue
        owner = meta.get("owner_user_id")
        if owner in (None, ""):
            continue
        try:
            counts[int(owner)] = counts.get(int(owner), 0) + 1
        except (TypeError, ValueError):
            continue
    for user in users:
        user.projects = counts.get(user.user_id, 0)
    return users
