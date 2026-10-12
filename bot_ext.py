from __future__ import annotations

"""Расширения бота V3: админ-команды владельца, пуш-уведомления, Mini App.

Подключается из bot.py: bot_ext.attach(application) + post_init в builder.
"""

import asyncio
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

import billing
import notify
from settings import Settings

logger = logging.getLogger("aiagent.bot_ext")


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _admin(settings: Settings, user_id: int) -> bool:
    return notify.is_admin(settings, user_id)


async def post_init(application: Application) -> None:
    """Внутри event loop бота: включаем отправку уведомлений из любых потоков."""
    notify.init_sender(application.bot, asyncio.get_running_loop())
    jobs = application.bot_data.get("jobs")
    if jobs is not None:
        settings = application.bot_data["settings"]
        jobs.on_event = lambda event: notify.job_event(settings, event)
    logger.info("notify: sender готов, job-события → владелец")


def attach(application: Application) -> None:
    application.add_handler(CommandHandler("paid", cmd_paid))
    application.add_handler(CommandHandler("unpaid", cmd_unpaid))
    application.add_handler(CommandHandler("users", cmd_users))
    application.add_handler(CommandHandler("app", cmd_app))
    application.add_error_handler(error_notify_handler)


def app_keyboard(settings: Settings) -> InlineKeyboardMarkup | None:
    if not settings.miniapp_url:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚀 Открыть приложение", web_app=WebAppInfo(url=settings.miniapp_url))]]
    )


async def maybe_set_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Закрепляет Mini App в меню чата (кнопка слева от поля ввода)."""
    settings = _settings(context)
    if not settings.miniapp_url or not update.effective_chat:
        return
    try:
        await context.application.bot.set_chat_menu_button(
            chat_id=update.effective_chat.id,
            menu_button=MenuButtonWebApp(text="🚀 Приложение", web_app=WebAppInfo(url=settings.miniapp_url)),
        )
    except Exception:
        logger.debug("menu button не установлен", exc_info=True)


async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None or not _admin(settings, user.id):
        await update.effective_message.reply_text("⛔ Эта команда только для владельца бота.")
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text(
            "Использование:\n/paid 123456 — доступ навсегда\n/paid 123456 30 — доступ на 30 дней\n/unpaid 123456 — отключить"
        )
        return
    target = int(args[0])
    days = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    until = billing.grant_paid(target, days, notes=f"/paid от {user.id}")
    billing.log_event("payment_granted", f"/paid {days or 'forever'} until {until[:10]}", user_id=target)
    notify.user_message(target, notify.payment_granted_text(until))
    await update.effective_message.reply_text(
        f"✅ Пользователю <code>{target}</code> открыт доступ до {until[:10]}.\nЕму отправлено уведомление.",
        parse_mode="HTML",
    )


async def cmd_unpaid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None or not _admin(settings, user.id):
        await update.effective_message.reply_text("⛔ Эта команда только для владельца бота.")
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text("Использование: /unpaid 123456")
        return
    target = int(args[0])
    billing.revoke_paid(target)
    billing.log_event("payment_revoked", "", user_id=target)
    notify.user_message(target, "Доступ по подписке отключён. Напиши владельцу, если это ошибка.")
    await update.effective_message.reply_text(f"🚫 У пользователя <code>{target}</code> доступ отключён.", parse_mode="HTML")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    user = update.effective_user
    if user is None or not _admin(settings, user.id):
        await update.effective_message.reply_text("⛔ Эта команда только для владельца бота.")
        return
    rows = billing.list_users(settings)
    if not rows:
        await update.effective_message.reply_text("Пользователей пока нет.")
        return
    lines = [f"👥 Пользователей: {len(rows)}", ""]
    for row in rows[:40]:
        paid = "💎 оплачено" if billing.is_paid(row.user_id) else "🆓 free"
        name = row.first_name or row.username or str(row.user_id)
        until = f" до {row.paid_until[:10]}" if row.paid_until and not row.paid_until.startswith('9999') else ""
        lines.append(f"• <code>{row.user_id}</code> {name} — {paid}{until}, работ: {row.projects}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    kb = app_keyboard(settings)
    if kb is None:
        await update.effective_message.reply_text(
            "MINIAPP_URL не настроен. Запусти сервер (он поднимается вместе с ботом) и укажи публичный HTTPS-адрес в config.env."
        )
        return
    await update.effective_message.reply_text("🚀 Управляй работами в приложении:", reply_markup=kb)
    await maybe_set_menu_button(update, context)


async def error_notify_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Каждая необработанная ошибка Telegram-цикла → мгновенный пуш владельцу с кодом."""
    if context.error:
        try:
            notify.error_notify(_settings(context), context.error)
        except Exception:
            logger.exception("error_notify failed")
