from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from settings import DB_PATH


def _connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_state (
            user_id INTEGER PRIMARY KEY,
            selected_project TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def get_selected_project(user_id: int) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT selected_project FROM user_state WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def set_selected_project(user_id: int, project_name: str | None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_state(user_id, selected_project, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                selected_project = excluded.selected_project,
                updated_at = excluded.updated_at
            """,
            (int(user_id), project_name, now),
        )
        conn.commit()
