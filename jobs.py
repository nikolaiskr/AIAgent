from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from settings import DB_PATH

logger = logging.getLogger("aiagent.jobs")


class JobCancelled(RuntimeError):
    pass


class ProjectBusyError(RuntimeError):
    """По проекту уже выполняется задача — ждать завершения или отменить её."""


@dataclass(slots=True)
class JobInfo:
    id: str
    user_id: int
    project: str
    kind: str
    status: str
    progress: str
    created_at: str
    updated_at: str
    result_json: str | None = None
    error: str | None = None


class JobManager:
    """Локальная очередь V3. Не блокирует Telegram event loop и поддерживает cancel.

    Гарантирует, что по одному проекту одновременно идёт не больше одной задачи
    (ProjectBusyError при попытке запустить вторую). on_event получает события
    job-цикла для пуш-уведомлений владельцу.
    """

    def __init__(self, workers: int = 2, on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="aiagent-worker")
        self._cancel: dict[str, threading.Event] = {}
        self._futures: dict[str, Future] = {}
        self._project_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.on_event = on_event
        _init_db()

    def _lock_for(self, project: str) -> threading.Lock:
        with self._locks_guard:
            return self._project_locks.setdefault(project, threading.Lock())

    def submit(
        self,
        *,
        user_id: int,
        project: str,
        kind: str,
        func: Callable[[Callable[[str], None]], Any],
    ) -> str:
        lock = self._lock_for(project)
        if not lock.acquire(blocking=False):
            raise ProjectBusyError(
                f"По работе «{project}» уже выполняется задача. Дождись её завершения или нажми «❌ Отменить»."
            )
        job_id = uuid.uuid4().hex[:12]
        event = threading.Event()
        self._cancel[job_id] = event
        now = _now()
        _upsert(JobInfo(job_id, int(user_id), project, kind, "queued", "В очереди", now, now))

        def progress(message: str) -> None:
            if event.is_set():
                raise JobCancelled("Операция отменена пользователем.")
            _update(job_id, status="running", progress=message)

        def run():
            started = time.monotonic()
            _update(job_id, status="running", progress="Запущено")
            try:
                result = func(progress)
                if event.is_set():
                    raise JobCancelled("Операция отменена пользователем.")
                _update(job_id, status="completed", progress="Готово", result_json=_serialize_result(result))
                self._emit("completed", job_id, kind, project, user_id, time.monotonic() - started)
                return result
            except JobCancelled as exc:
                _update(job_id, status="cancelled", progress="Отменено", error=str(exc))
                self._emit("cancelled", job_id, kind, project, user_id, time.monotonic() - started)
                raise
            except Exception as exc:
                logger.exception("Job %s (%s) failed", job_id, kind)
                _update(job_id, status="failed", progress="Ошибка", error=str(exc))
                self._emit("failed", job_id, kind, project, user_id, time.monotonic() - started, error=str(exc))
                raise
            finally:
                lock.release()
                self._futures.pop(job_id, None)
                self._cancel.pop(job_id, None)

        future = self.executor.submit(run)
        self._futures[job_id] = future
        return job_id

    def _emit(self, event: str, job_id: str, kind: str, project: str, user_id: int,
              duration_s: float, error: str | None = None) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event({
                "event": event,
                "job_id": job_id,
                "kind": kind,
                "project": project,
                "user_id": int(user_id),
                "duration_s": round(duration_s, 1),
                "error": error,
            })
        except Exception:
            logger.exception("on_event callback failed")

    def cancel(self, job_id: str) -> bool:
        event = self._cancel.get(job_id)
        if event is None:
            return False
        event.set()
        future = self._futures.get(job_id)
        if future and future.cancel():
            _update(job_id, status="cancelled", progress="Отменено до запуска")
        else:
            _update(job_id, progress="Получен запрос отмены")
        return True

    def cancel_user_jobs(self, user_id: int) -> int:
        """Отменяет все активные job'ы пользователя. Возвращает число запросов отмены."""
        count = 0
        for job_id, future in list(self._futures.items()):
            info = get_job(job_id)
            if info and int(info.user_id) == int(user_id) and info.status in {"queued", "running"}:
                if self.cancel(job_id):
                    count += 1
        return count

    def future(self, job_id: str) -> Future | None:
        return self._futures.get(job_id)

    def info(self, job_id: str) -> JobInfo | None:
        return get_job(job_id)


def get_job(job_id: str) -> JobInfo | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id,user_id,project,kind,status,progress,created_at,updated_at,result_json,error FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    return JobInfo(*row) if row else None


def latest_user_job(user_id: int) -> JobInfo | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id,user_id,project,kind,status,progress,created_at,updated_at,result_json,error FROM jobs WHERE user_id=? ORDER BY updated_at DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
    return JobInfo(*row) if row else None


def recent_jobs(user_id: int, limit: int = 10) -> list[JobInfo]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id,user_id,project,kind,status,progress,created_at,updated_at,result_json,error FROM jobs WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
    return [JobInfo(*row) for row in rows]


def _serialize_result(result: Any) -> str:
    try:
        if hasattr(result, "__dict__"):
            data = {k: str(v) for k, v in result.__dict__.items()}
        else:
            data = str(result)
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return json.dumps({"result": str(result)}, ensure_ascii=False)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                project TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                progress TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result_json TEXT,
                error TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, updated_at)")
        conn.commit()


def _upsert(info: JobInfo) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO jobs(id,user_id,project,kind,status,progress,created_at,updated_at,result_json,error)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (info.id, info.user_id, info.project, info.kind, info.status, info.progress, info.created_at, info.updated_at, info.result_json, info.error),
        )
        conn.commit()


def _update(job_id: str, *, status: str | None = None, progress: str | None = None, result_json: str | None = None, error: str | None = None) -> None:
    """Один UPDATE без предварительного SELECT — быстро и без лишних блокировок."""
    sets: list[str] = ["updated_at=?"]
    params: list[Any] = [_now()]
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if progress is not None:
        sets.append("progress=?")
        params.append(progress)
    if result_json is not None:
        sets.append("result_json=?")
        params.append(result_json)
    if error is not None:
        sets.append("error=?")
        params.append(error)
    params.append(job_id)
    try:
        with _connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", params)
            conn.commit()
    except sqlite3.Error:
        logger.debug("jobs update skipped: %s", traceback.format_exc(limit=1))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
