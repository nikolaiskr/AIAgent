from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from settings import DB_PATH


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    return conn


def log_usage(model: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
    """Пишет потребление токенов одним INSERT; ошибки не прерывают pipeline."""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO usage(ts, model, prompt_tokens, completion_tokens) VALUES(?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), model[:80], int(prompt_tokens or 0), int(completion_tokens or 0)),
            )
            conn.commit()
    except Exception:
        pass


def usage_totals() -> dict[str, Any]:
    """Сводка по токенам: суммарно и по моделям. Для админ-панели."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT model,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt,
                       COALESCE(SUM(completion_tokens), 0) AS completion,
                       COUNT(*) AS calls
                FROM usage GROUP BY model ORDER BY prompt + completion DESC
                """
            ).fetchall()
    except Exception:
        rows = []
    models = [
        {"model": r[0], "prompt_tokens": r[1], "completion_tokens": r[2], "calls": r[3]}
        for r in rows
    ]
    return {
        "prompt_tokens": sum(m["prompt_tokens"] for m in models),
        "completion_tokens": sum(m["completion_tokens"] for m in models),
        "calls": sum(m["calls"] for m in models),
        "models": models,
    }
