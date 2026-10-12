from __future__ import annotations

import asyncio
import hashlib
import logging
import traceback
from typing import Any

from billing import log_event

logger = logging.getLogger("aiagent.notify")

_bot: Any = None
_loop: asyncio.AbstractEventLoop | None = None


def init_sender(bot: Any, loop: asyncio.AbstractEventLoop) -> None:
    """Вызывается из post_init бота: даёт модулю возможность слать сообщения из любого потока."""
    global _bot, _loop
    _bot = bot
    _loop = loop


def owner_ids(settings: Any) -> list[int]:
    ids = sorted(getattr(settings, "admin_ids", set()) or set())
    return ids


def is_admin(settings: Any, user_id: int) -> bool:
    return int(user_id) in set(getattr(settings, "admin_ids", set()) or set())


def owner_link(settings: Any) -> str:
    """Ссылка для кнопки «написать владельцу»."""
    explicit = getattr(settings, "owner_link", None)
    if explicit:
        return explicit
    admins = owner_ids(settings)
    if admins:
        return f"tg://user?id={admins[0]}"
    return "tg://user?id=0"


def _send(chat_id: int, text: str) -> None:
    if _bot is None or _loop is None:
        logger.info("notify (no sender) → %s: %s", chat_id, text[:160])
        return
    try:
        asyncio.run_coroutine_threadsafe(
            _bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True),
            _loop,
        )
    except Exception:
        logger.exception("notify send failed")


def owner_message(settings: Any, text: str, *, kind: str = "owner", user_id: int | None = None,
                  project: str | None = None) -> None:
    """Красивое пуш-уведомление всем владельцам (ADMIN_IDS) + запись в ленту событий."""
    log_event(kind, text.replace("<b>", "").replace("</b>", "").replace("<code>", "`").replace("</code>", "`"),
              user_id=user_id, project=project)
    for admin_id in owner_ids(settings):
        _send(admin_id, text)


def user_message(user_id: int, text: str) -> None:
    _send(int(user_id), text)


def user_link(user_id: int, username: str | None, first_name: str | None) -> str:
    label = first_name or username or str(user_id)
    return f'<a href="tg://user?id={int(user_id)}">{label}</a>'


def err_code(exc: BaseException | str) -> str:
    """Короткий код ошибки для перекрёстных ссылок «пользователь↔владелец↔лог»."""
    raw = str(exc) if not isinstance(exc, str) else exc
    return "ERR-" + hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:6].upper()


def job_event(settings: Any, event: dict[str, Any]) -> None:
    """Пуш-уведомления владельцу о событиях job-цикла (wired as JobManager.on_event)."""
    if not getattr(settings, "notify_job_events", True):
        return
    kind = str(event.get("kind"))
    project = str(event.get("project"))
    duration = _fmt_duration(float(event.get("duration_s") or 0))
    state = event.get("event")
    if state == "completed":
        owner_message(
            settings,
            f"✅ <b>{kind}</b>\n📚 {project}\n⏱ {duration}",
            kind="job_completed",
            user_id=event.get("user_id"),
            project=project,
        )
    elif state == "failed":
        error = str(event.get("error") or "unknown")
        code = err_code(error)
        owner_message(
            settings,
            f"❌ <b>{kind}</b> · 📚 {project}\n⏱ {duration}\n"
            f"👤 {user_link(event.get('user_id') or 0, None, None)}\n"
            f"Код: <code>{code}</code>\n"
            f"Ошибка: {error[:600]}",
            kind="job_failed",
            user_id=event.get("user_id"),
            project=project,
        )
    elif state == "cancelled":
        owner_message(
            settings,
            f"⛔ <b>{kind}</b> отменён пользователем\n📚 {project}",
            kind="job_cancelled",
            user_id=event.get("user_id"),
            project=project,
        )


def error_notify(settings: Any, error: BaseException) -> None:
    """Необработанные ошибки Telegram-цикла — сразу владельцу, с traceback."""
    tb = traceback.format_exc()
    code = err_code(error)
    owner_message(
        settings,
        f"🐛 <b>Необработанная ошибка бота</b>\nКод: <code>{code}</code>\n"
        f"<code>{str(error)[:800]}</code>\n\n traceback: logs/agent.log",
        kind="bot_error",
    )
    logger.error("bot error %s\n%s", code, tb)


def payment_request(settings: Any, user_id: int, username: str | None, first_name: str | None,
                    used: int, limit: int) -> None:
    owner_message(
        settings,
        f"💰 <b>Запрос оплаты</b>\n"
        f"👤 {user_link(user_id, username, first_name)}\n"
        f"id: <code>{user_id}</code>\n"
        f"Лимит: {used}/{limit} работ\n"
        f"Подтвердить: <code>/paid {user_id}</code>",
        kind="payment_request",
        user_id=user_id,
    )


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}с"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}м {sec}с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}ч {minutes}м"


def payment_granted_text(paid_until: str) -> str:
    if paid_until.startswith("9999"):
        return "🎉 Оплата подтверждена! Теперь у тебя <b>безлимитный доступ</b>. Пиши сколько угодно работ."
    return f"🎉 Оплата подтверждена! Доступ открыт до <b>{paid_until[:10]}</b>."


def payment_required_text(used: int, limit: int) -> str:
    return (
        f"💳 <b>Бесплатный лимит исчерпан</b>\n\n"
        f"Создано работ: {used} из {limit}.\n"
        f"Чтобы продолжить, напиши владельцу бота — он подтвердит оплату и откроет доступ автоматически."
    )
