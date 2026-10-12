from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent import AgentError, check_formatting, list_docx_files, solve_task
from assembler import AssemblyError, assemble_work
import billing
import bot_ext
from diagnostics import run_diagnostics
from jobs import JobCancelled, JobManager, ProjectBusyError, latest_user_job
from pipeline import (
    PipelineError,
    continue_section,
    create_work_plan,
    ensure_requirements,
    generate_all_sections,
    generate_chapter,
    generate_section,
    revise_section,
    section_candidates,
)
from projects import (
    ROLE_LABELS,
    WORK_TYPES,
    ProjectError,
    create_project,
    delete_material,
    ensure_project_owner,
    get_project,
    list_materials,
    list_projects_for_user,
    load_project_meta,
    materials_text,
    prepare_upload_target,
    project_summary,
    readiness_text,
    register_uploaded_file,
    set_material_role,
    update_project_field,
)
from readers import IMAGE_EXTENSIONS, UPLOAD_EXTENSIONS
from requirements_engine import load_requirements, requirements_summary
from settings import ConfigError, Settings, load_settings
from storage import get_selected_project, set_selected_project
from workspace import (
    WorkPlan, latest_section_version, list_section_versions, load_work_plan, plan_text,
    save_work_plan, section_dir, set_section_status,
)


logger = logging.getLogger("aiagent.bot")

BTN_NEW = "🆕 Новая работа"
BTN_SELECT = "📁 Выбрать работу"
BTN_PROJECT = "📋 Проект"
BTN_MATERIALS = "📎 Материалы"
BTN_PLAN = "🗺 План"
BTN_GENERATE = "📝 Продолжить"
BTN_FEEDBACK = "💬 Замечания"
BTN_ASSEMBLE = "📕 Собрать работу"
BTN_CHECK = "🔍 Проверить"
BTN_DIAG = "⚙️ Диагностика"
BTN_CANCEL = "❌ Отменить"

MENU = ReplyKeyboardMarkup(
    [
        [BTN_NEW, BTN_SELECT],
        [BTN_PROJECT, BTN_MATERIALS],
        [BTN_PLAN, BTN_GENERATE],
        [BTN_FEEDBACK, BTN_ASSEMBLE],
        [BTN_CHECK, BTN_DIAG],
        [BTN_CANCEL],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def app_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def job_manager(context: ContextTypes.DEFAULT_TYPE) -> JobManager:
    return context.application.bot_data["jobs"]


def uid(update: Update) -> int | None:
    return update.effective_user.id if update.effective_user else None


def selected_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    cached = context.user_data.get("selected_project")
    if cached:
        return str(cached)
    user = uid(update)
    if user is None:
        return None
    value = get_selected_project(user)
    if value:
        context.user_data["selected_project"] = value
    return value


def persist_selected(update: Update, context: ContextTypes.DEFAULT_TYPE, project_name: str | None) -> None:
    user = uid(update)
    if project_name:
        context.user_data["selected_project"] = project_name
    else:
        context.user_data.pop("selected_project", None)
    if user is not None:
        set_selected_project(user, project_name)


def reset_mode(context: ContextTypes.DEFAULT_TYPE) -> None:
    keys = [
        "mode", "new_work_type", "role_target", "delete_target", "project_field_name",
        "section_choices", "chapter_choices", "selected_section_id", "feedback_section_id",
        "check_choices", "material_choices", "project_choices", "force_assemble",
    ]
    for key in keys:
        context.user_data.pop(key, None)


async def authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings = app_settings(context)
    user = uid(update)
    if user is None:
        return False
    if settings.allowed_user_ids and user not in settings.allowed_user_ids:
        if update.callback_query:
            await update.callback_query.answer("Доступ закрыт", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("⛔ Доступ к этому боту закрыт.")
        return False
    return True


async def require_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    project = selected_project(update, context)
    if not project:
        if update.callback_query:
            await update.callback_query.answer("Сначала выбери работу", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Сначала создай или выбери работу.", reply_markup=MENU)
        return None
    user = uid(update)
    try:
        if user is not None:
            await asyncio.to_thread(ensure_project_owner, app_settings(context), project, user)
        await asyncio.to_thread(get_project, app_settings(context), project)
    except Exception as exc:
        persist_selected(update, context, None)
        if update.effective_message:
            await update.effective_message.reply_text(f"Не удалось открыть выбранную работу: {exc}", reply_markup=MENU)
        return None
    return project


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    reset_mode(context)
    settings = app_settings(context)
    user = uid(update)
    if user is not None:
        try:
            is_new = await asyncio.to_thread(
                billing.touch_user, user,
                update.effective_user.username if update.effective_user else None,
                update.effective_user.first_name if update.effective_user else None,
            )
            if is_new:
                from notify import owner_message, user_link
                owner_message(
                    settings,
                    f"🆕 <b>Новый пользователь</b>\n"
                    f"👤 {user_link(user, update.effective_user.username if update.effective_user else None, update.effective_user.first_name if update.effective_user else None)}\n"
                    f"id: <code>{user}</code>",
                    kind="new_user",
                    user_id=user,
                )
        except Exception:
            logger.debug("touch_user failed", exc_info=True)
    project = selected_project(update, context)
    if project:
        try:
            if user is not None:
                await asyncio.to_thread(ensure_project_owner, settings, project, user)
        except Exception:
            persist_selected(update, context, None)
            project = None
    ai = "✅ AI подключён" if settings.any_ai_ready else "⚪ AI ещё не подключён; workspace и файлы работают локально"
    current = f"\nТекущая работа: «{project}»" if project else "\nРабота пока не выбрана"
    await update.effective_message.reply_text(
        "🎓 AI Agent V3\n"
        "Курсовая теперь ведётся как долгоживущий проект: материалы → требования → план → разделы → версии → сборка.\n"
        f"{ai}{current}",
        reply_markup=MENU,
    )
    kb = bot_ext.app_keyboard(settings)
    if kb is not None:
        await update.effective_message.reply_text("Удобнее всего работать в приложении:", reply_markup=kb)
        await bot_ext.maybe_set_menu_button(update, context)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"Твой Telegram user_id: {uid(update)}", reply_markup=MENU)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    user = uid(update)
    if user is not None:
        cancelled = job_manager(context).cancel_user_jobs(user)
        if cancelled:
            reset_mode(context)
            plural = "задач" if cancelled > 1 else "задачи"
            await update.effective_message.reply_text(
                f"❌ Запрошена отмена {cancelled} {plural}. Остановка произойдёт на ближайшем безопасном этапе.",
                reply_markup=MENU,
            )
            return
    reset_mode(context)
    await update.effective_message.reply_text("Текущее действие отменено.", reply_markup=MENU)


# ---------- Project creation / selection ----------

async def new_work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    reset_mode(context)
    rows = [[InlineKeyboardButton(label, callback_data=f"newtype:{code}")] for code, label in WORK_TYPES.items()]
    await update.effective_message.reply_text("Какой тип работы создаём?", reply_markup=InlineKeyboardMarkup(rows))


async def new_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    work_type = q.data.split(":", 1)[1]
    context.user_data["new_work_type"] = work_type
    context.user_data["mode"] = "new_project_name"
    await q.edit_message_text(f"Выбрано: {WORK_TYPES.get(work_type, work_type)}\n\nНапиши фамилию или короткое название, например: Бего")


async def choose_work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    user = uid(update)
    try:
        projects = await asyncio.to_thread(list_projects_for_user, app_settings(context), int(user))
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось получить работы: {exc}", reply_markup=MENU)
        return
    if not projects:
        await update.effective_message.reply_text("Работ пока нет. Нажми «Новая работа».", reply_markup=MENU)
        return
    names = [p.name for p in projects]
    context.user_data["project_choices"] = names
    rows = [[InlineKeyboardButton(name[:55], callback_data=f"folder:{i}")] for i, name in enumerate(names[:80])]
    await update.effective_message.reply_text("Выбери работу:", reply_markup=InlineKeyboardMarkup(rows))


async def folder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    try:
        idx = int(q.data.split(":", 1)[1])
        name = context.user_data.get("project_choices", [])[idx]
        await asyncio.to_thread(ensure_project_owner, app_settings(context), name, int(uid(update)))
        await asyncio.to_thread(load_project_meta, app_settings(context), name)
    except Exception as exc:
        await q.edit_message_text(f"Не удалось открыть работу: {exc}")
        return
    persist_selected(update, context, name)
    reset_mode(context)
    await q.edit_message_text(f"✅ Выбрана работа: «{name}».")
    await q.message.reply_text(await _project_panel_text(context, name), reply_markup=MENU)


async def project_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    project = await require_project(update, context)
    if not project:
        return
    rows = [
        [InlineKeyboardButton("✏️ Тема", callback_data="projectfield:topic"), InlineKeyboardButton("🔢 Вариант", callback_data="projectfield:variant")],
        [InlineKeyboardButton("📘 Анализ требований", callback_data="requirements:analyze"), InlineKeyboardButton("📚 Разделы", callback_data="project:sections")],
    ]
    await update.effective_message.reply_text(await _project_panel_text(context, project), reply_markup=InlineKeyboardMarkup(rows))


async def project_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    field = q.data.split(":", 1)[1]
    context.user_data["mode"] = "project_field"
    context.user_data["project_field_name"] = field
    await q.edit_message_text("Напиши новое значение. Если поле не требуется — отправь «нет».")




async def project_sections_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    if not project:
        return
    leaves = await asyncio.to_thread(section_candidates, app_settings(context), project, leaves_only=True)
    if not leaves:
        await q.edit_message_text("Разделов пока нет. Сначала создай план.")
        return
    context.user_data["section_view_choices"] = [x.id for x in leaves]
    rows = []
    project_path = get_project(app_settings(context), project)
    for i, node in enumerate(leaves[:70]):
        ver, _ = await asyncio.to_thread(latest_section_version, project_path, node)
        status = ver.status if ver else node.status
        icon = "✅" if status in {"approved", "completed"} else "🟡" if status in {"draft", "needs_revision"} else "⚪"
        version = f" v{ver.version}" if ver else ""
        rows.append([InlineKeyboardButton(f"{icon} {node.id} {node.title}{version}"[:62], callback_data=f"sectionview:{i}")])
    await q.edit_message_text("Разделы работы:", reply_markup=InlineKeyboardMarkup(rows))


async def section_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    try:
        idx = int(q.data.split(":", 1)[1])
        sid = context.user_data.get("section_view_choices", [])[idx]
    except Exception:
        await q.edit_message_text("Список устарел.")
        return
    nodes = await asyncio.to_thread(section_candidates, app_settings(context), project, leaves_only=True)
    node = next((x for x in nodes if x.id == sid), None)
    if node is None:
        await q.edit_message_text("Раздел не найден.")
        return
    context.user_data["view_section_id"] = sid
    project_path = get_project(app_settings(context), project)
    version, text = await asyncio.to_thread(latest_section_version, project_path, node)
    versions = await asyncio.to_thread(list_section_versions, project_path, node)
    if version:
        detail = (
            f"{node.id} {node.title}\n"
            f"Текущая версия: v{version.version}\n"
            f"Статус: {version.status}\n"
            f"Слов: {version.word_count}\n"
            f"Всего версий: {len(versions)}"
        )
    else:
        detail = f"{node.id} {node.title}\nВерсий пока нет.\nСтатус: {node.status}"
    rows = []
    if version:
        rows.append([InlineKeyboardButton("📥 Скачать", callback_data="section:download"), InlineKeyboardButton("↩️ Версии", callback_data="section:versions")])
        rows.append([InlineKeyboardButton("✅ Утвердить", callback_data="section:approve"), InlineKeyboardButton("🧩 Продолжить", callback_data="section:continue")])
        rows.append([InlineKeyboardButton("💬 Учесть замечание", callback_data="section:feedback")])
    else:
        rows.append([InlineKeyboardButton("✨ Создать", callback_data="section:create")])
    await q.edit_message_text(detail, reply_markup=InlineKeyboardMarkup(rows))


async def section_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    sid = str(context.user_data.get("view_section_id") or "")
    if not project or not sid:
        await q.edit_message_text("Контекст раздела устарел.")
        return
    nodes = await asyncio.to_thread(section_candidates, app_settings(context), project, leaves_only=True)
    node = next((x for x in nodes if x.id == sid), None)
    if node is None:
        await q.edit_message_text("Раздел не найден.")
        return
    action = q.data.split(":", 1)[1]
    project_path = get_project(app_settings(context), project)
    version, _ = await asyncio.to_thread(latest_section_version, project_path, node)
    if action == "download":
        if not version or not version.docx_file:
            await q.edit_message_text("Версии ещё нет.")
            return
        path = section_dir(project_path, node) / version.docx_file
        with path.open("rb") as fh:
            await q.message.reply_document(fh, filename=f"{node.id}_{node.title}_v{version.version}.docx")
        return
    if action == "approve":
        if not version:
            await q.edit_message_text("Сначала создай раздел.")
            return
        await asyncio.to_thread(set_section_status, project_path, node, "approved")
        await q.edit_message_text(f"✅ {node.id} отмечен approved.")
        return
    if action == "continue":
        context.user_data["selected_section_id"] = sid
        context.user_data["mode"] = "continue_section_instruction"
        await q.edit_message_text("Что добавить или изменить? Будет создана новая версия, предыдущая сохранится.")
        return
    if action == "feedback":
        context.user_data["feedback_section_id"] = sid
        context.user_data["mode"] = "feedback_text"
        await q.edit_message_text("Напиши замечание преподавателя для этого раздела.")
        return
    if action == "create":
        context.user_data["selected_section_id"] = sid
        context.user_data["mode"] = "generate_section_instruction"
        await q.edit_message_text("Дополнительная инструкция или «-» для генерации строго по плану.")
        return
    if action == "versions":
        versions = await asyncio.to_thread(list_section_versions, project_path, node)
        context.user_data["version_choices"] = [v.version for v in versions]
        rows = [[InlineKeyboardButton(f"v{v.version} · {v.status} · {v.reason}"[:60], callback_data=f"sectionver:{i}")] for i, v in enumerate(reversed(versions[-20:]))]
        # store same reversed list to keep indexes aligned
        context.user_data["version_choices"] = [v.version for v in reversed(versions[-20:])]
        await q.edit_message_text(f"История версий {node.id}:", reply_markup=InlineKeyboardMarkup(rows))


async def section_version_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    sid = str(context.user_data.get("view_section_id") or "")
    try:
        idx = int(q.data.split(":", 1)[1])
        vnum = context.user_data.get("version_choices", [])[idx]
    except Exception:
        await q.edit_message_text("История устарела.")
        return
    nodes = await asyncio.to_thread(section_candidates, app_settings(context), project, leaves_only=True)
    node = next((x for x in nodes if x.id == sid), None)
    if not node:
        await q.edit_message_text("Раздел не найден.")
        return
    versions = await asyncio.to_thread(list_section_versions, get_project(app_settings(context), project), node)
    ver = next((v for v in versions if v.version == vnum), None)
    if not ver or not ver.docx_file:
        await q.edit_message_text("Версия не найдена.")
        return
    path = section_dir(get_project(app_settings(context), project), node) / ver.docx_file
    with path.open("rb") as fh:
        await q.message.reply_document(fh, filename=f"{node.id}_{node.title}_v{ver.version}.docx")


async def requirements_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    if not project:
        return
    status = await q.edit_message_text("📘 Анализирую требования…")
    try:
        req = await asyncio.to_thread(ensure_requirements, app_settings(context), project, force=True)
        await status.edit_text(requirements_summary(req)[:4000])
        await q.message.reply_text("Меню:", reply_markup=MENU)
    except Exception as exc:
        await q.message.reply_text(f"Ошибка анализа требований: {exc}", reply_markup=MENU)


# ---------- Materials ----------

async def materials_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    project = await require_project(update, context)
    if not project:
        return
    text = await asyncio.to_thread(materials_text, app_settings(context), project)
    rows = [
        [InlineKeyboardButton("➕ Добавить", callback_data="materials:add"), InlineKeyboardButton("🔄 Обновить", callback_data="materials:list")],
        [InlineKeyboardButton("🏷 Роли", callback_data="materials:roles"), InlineKeyboardButton("🗑 Удалить", callback_data="materials:delete")],
    ]
    await update.effective_message.reply_text(text[:4000], reply_markup=InlineKeyboardMarkup(rows))


async def materials_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    if not project:
        return
    action = q.data.split(":", 1)[1]
    files = await asyncio.to_thread(list_materials, app_settings(context), project)
    if action == "add":
        context.user_data["mode"] = "upload_materials"
        await q.edit_message_text("Отправляй PDF/DOCX/XLSX/TXT/MD/JPG/PNG/WEBP. Файлы автоматически сохраняются в input/ и не перезаписывают предыдущие версии.")
        return
    if action == "list":
        await q.edit_message_text((await asyncio.to_thread(materials_text, app_settings(context), project))[:4000])
        return
    if not files:
        await q.edit_message_text("Материалов пока нет.")
        return
    context.user_data["material_choices"] = [str(x["path"]) for x in files[:50]]
    prefix = "matrole" if action == "roles" else "matdel"
    rows = [[InlineKeyboardButton(str(x["name"])[:50], callback_data=f"{prefix}:{i}")] for i, x in enumerate(files[:50])]
    await q.edit_message_text("Выбери материал:", reply_markup=InlineKeyboardMarkup(rows))


async def material_role_file_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    try:
        idx = int(q.data.split(":", 1)[1])
        target = context.user_data.get("material_choices", [])[idx]
    except Exception:
        await q.edit_message_text("Список устарел.")
        return
    context.user_data["role_target"] = target
    rows = [[InlineKeyboardButton(label, callback_data=f"setrole:{code}")] for code, label in ROLE_LABELS.items()]
    await q.edit_message_text(f"Какую роль назначить файлу {Path(target).name}?", reply_markup=InlineKeyboardMarkup(rows))


async def set_role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    target = context.user_data.get("role_target")
    role = q.data.split(":", 1)[1]
    if not project or not target:
        await q.edit_message_text("Контекст устарел.")
        return
    try:
        await asyncio.to_thread(set_material_role, app_settings(context), project, str(target), role)
        context.user_data.pop("role_target", None)
        await q.edit_message_text(f"✅ Роль назначена: {ROLE_LABELS.get(role, role)}. Файл перемещён в соответствующую input-папку.")
    except Exception as exc:
        await q.edit_message_text(f"Ошибка: {exc}")


async def material_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    try:
        idx = int(q.data.split(":", 1)[1])
        target = context.user_data.get("material_choices", [])[idx]
    except Exception:
        await q.edit_message_text("Список устарел.")
        return
    context.user_data["delete_target"] = target
    await q.edit_message_text(
        f"Удалить {Path(target).name}?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Да", callback_data="del:yes"), InlineKeyboardButton("Нет", callback_data="del:no")]]),
    )


async def delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    if q.data == "del:no":
        context.user_data.pop("delete_target", None)
        await q.edit_message_text("Удаление отменено.")
        return
    project = await require_project(update, context)
    target = context.user_data.pop("delete_target", None)
    if not project or not target:
        await q.edit_message_text("Контекст устарел.")
        return
    try:
        await asyncio.to_thread(delete_material, app_settings(context), project, str(target))
        await q.edit_message_text("✅ Материал удалён.")
    except Exception as exc:
        await q.edit_message_text(f"Ошибка удаления: {exc}")


async def handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    project = await require_project(update, context)
    if not project:
        return
    message = update.effective_message
    settings = app_settings(context)
    try:
        if message.document:
            obj = message.document
            filename = obj.file_name or f"document_{datetime.now():%Y%m%d_%H%M%S}"
            ext = Path(filename).suffix.lower()
            if ext not in UPLOAD_EXTENSIONS:
                await message.reply_text("Формат не поддерживается: " + ext, reply_markup=MENU)
                return
            if obj.file_size and obj.file_size > settings.max_upload_mb * 1024 * 1024:
                await message.reply_text(f"Файл больше лимита {settings.max_upload_mb} МБ.", reply_markup=MENU)
                return
            file_id = obj.file_id
        elif message.photo:
            obj = message.photo[-1]
            filename = f"photo_{datetime.now():%Y%m%d_%H%M%S}_{obj.file_unique_id}.jpg"
            file_id = obj.file_id
        else:
            return
        target = await asyncio.to_thread(prepare_upload_target, settings, project, filename)
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=str(target))
        item = await asyncio.to_thread(register_uploaded_file, settings, project, target)
        role = ROLE_LABELS.get(str(item.get("role") or "other"), "❓ Другое")
        note = "\nVision распознает содержимое при первом анализе и закеширует результат." if target.suffix.lower() in IMAGE_EXTENSIONS else ""
        await message.reply_text(f"✅ {target.name}\nРоль: {role}{note}", reply_markup=MENU)
    except Exception as exc:
        logger.exception("Upload failed")
        await message.reply_text(f"Не удалось сохранить материал: {exc}", reply_markup=MENU)


# ---------- Plan ----------

async def plan_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    project = await require_project(update, context)
    if not project:
        return
    project_path = get_project(app_settings(context), project)
    plan = await asyncio.to_thread(load_work_plan, project_path)
    if plan is None:
        rows = [[InlineKeyboardButton("✨ Создать план", callback_data="plan:create")]]
        await update.effective_message.reply_text("План ещё не создан.", reply_markup=InlineKeyboardMarkup(rows))
        return
    rows = [
        [InlineKeyboardButton("🔄 Перестроить", callback_data="plan:rebuild"), InlineKeyboardButton("✏️ JSON", callback_data="plan:edit")],
        [InlineKeyboardButton("📥 work_plan.json", callback_data="plan:download")],
    ]
    await update.effective_message.reply_text(plan_text(plan)[:4000], reply_markup=InlineKeyboardMarkup(rows))


async def plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    if not project:
        return
    action = q.data.split(":", 1)[1]
    if action in {"create", "rebuild"}:
        await _submit_job(
            update,
            context,
            project,
            "create_plan",
            lambda progress: create_work_plan(app_settings(context), project, progress=progress, force=(action == "rebuild")),
            success_handler=_send_plan_result,
        )
        return
    path = get_project(app_settings(context), project) / "work_plan.json"
    if action == "download":
        if not path.exists():
            await q.edit_message_text("План не найден.")
            return
        with path.open("rb") as fh:
            await q.message.reply_document(fh, filename="work_plan.json")
        return
    if action == "edit":
        context.user_data["mode"] = "edit_plan_json"
        await q.edit_message_text("Отправь полный JSON нового плана. Текущий work_plan.json лучше сначала скачать. Старый файл будет заменён только если JSON валиден.")


# ---------- Generation / versions ----------

async def generate_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    project = await require_project(update, context)
    if not project:
        return
    rows = [
        [InlineKeyboardButton("📚 Работа по разделам", callback_data="gen:all")],
        [InlineKeyboardButton("📄 Отдельный раздел", callback_data="gen:section")],
        [InlineKeyboardButton("📑 Собрать главу", callback_data="gen:chapter")],
        [InlineKeyboardButton("🧩 Продолжить раздел", callback_data="gen:continue")],
        [InlineKeyboardButton("🧮 Отдельное задание", callback_data="gen:task")],
    ]
    await update.effective_message.reply_text("Что делаем?", reply_markup=InlineKeyboardMarkup(rows))


async def gen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    if not project:
        return
    action = q.data.split(":", 1)[1]
    settings = app_settings(context)
    if action == "all":
        await _submit_job(update, context, project, "generate_all", lambda progress: generate_all_sections(settings, project, progress=progress), success_handler=_send_multi_section_result)
        return
    if action == "task":
        context.user_data["mode"] = "standalone_task"
        await q.edit_message_text("Опиши задание и вариант одним сообщением.")
        return
    leaves = await asyncio.to_thread(section_candidates, settings, project, leaves_only=True)
    if not leaves:
        await q.edit_message_text("Нет work_plan. Сначала создай план.")
        return
    if action in {"section", "continue"}:
        context.user_data["section_choices"] = [x.id for x in leaves]
        prefix = "gensec" if action == "section" else "contsec"
        rows = [[InlineKeyboardButton(f"{x.id} {x.title}"[:60], callback_data=f"{prefix}:{i}")] for i, x in enumerate(leaves[:70]) if x.id.casefold() not in {"references", "literature"}]
        await q.edit_message_text("Выбери раздел:", reply_markup=InlineKeyboardMarkup(rows))
        return
    if action == "chapter":
        all_nodes = await asyncio.to_thread(section_candidates, settings, project, leaves_only=False)
        chapters = [x for x in all_nodes if x.children]
        context.user_data["chapter_choices"] = [x.id for x in chapters]
        rows = [[InlineKeyboardButton(f"{x.id} {x.title}"[:60], callback_data=f"genchap:{i}")] for i, x in enumerate(chapters[:40])]
        await q.edit_message_text("Какую главу собрать из уже созданных подразделов?", reply_markup=InlineKeyboardMarkup(rows))


async def gen_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    try:
        idx = int(q.data.split(":", 1)[1])
        sid = context.user_data.get("section_choices", [])[idx]
    except Exception:
        await q.edit_message_text("Список устарел.")
        return
    context.user_data["selected_section_id"] = sid
    context.user_data["mode"] = "generate_section_instruction"
    await q.edit_message_text(f"Раздел {sid}. Напиши дополнительную инструкцию или отправь «-», чтобы писать строго по плану.")


async def continue_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    try:
        idx = int(q.data.split(":", 1)[1])
        sid = context.user_data.get("section_choices", [])[idx]
    except Exception:
        await q.edit_message_text("Список устарел.")
        return
    context.user_data["selected_section_id"] = sid
    context.user_data["mode"] = "continue_section_instruction"
    await q.edit_message_text(f"Что добавить/изменить в текущей версии {sid}? Например: «добавь около страницы про теорию Герцберга». Новая версия не перезапишет старую.")


async def chapter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    try:
        idx = int(q.data.split(":", 1)[1])
        cid = context.user_data.get("chapter_choices", [])[idx]
    except Exception:
        await q.edit_message_text("Список устарел.")
        return
    await _submit_job(update, context, project, "generate_chapter", lambda progress: generate_chapter(app_settings(context), project, cid, progress=progress), success_handler=_send_chapter_result)


# ---------- Feedback ----------

async def feedback_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    project = await require_project(update, context)
    if not project:
        return
    leaves = await asyncio.to_thread(section_candidates, app_settings(context), project, leaves_only=True)
    if not leaves:
        await update.effective_message.reply_text("Сначала создай план.", reply_markup=MENU)
        return
    context.user_data["section_choices"] = [x.id for x in leaves]
    rows = [[InlineKeyboardButton(f"{x.id} {x.title}"[:60], callback_data=f"feedbacksec:{i}")] for i, x in enumerate(leaves[:70])]
    await update.effective_message.reply_text("Какого раздела касается замечание?", reply_markup=InlineKeyboardMarkup(rows))


async def feedback_section_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    try:
        idx = int(q.data.split(":", 1)[1])
        sid = context.user_data.get("section_choices", [])[idx]
    except Exception:
        await q.edit_message_text("Список устарел.")
        return
    context.user_data["feedback_section_id"] = sid
    context.user_data["mode"] = "feedback_text"
    await q.edit_message_text("Напиши замечание преподавателя. Будет создана новая версия только выбранного раздела; предыдущая сохранится.")


# ---------- Assembly / checks ----------

async def assemble_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    project = await require_project(update, context)
    if not project:
        return
    await _submit_job(update, context, project, "assemble_work", lambda progress: assemble_work(app_settings(context), project, allow_unapproved=False, progress=progress), success_handler=_send_assembly_result, assembly_retry=True)


async def force_assemble_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    if q.data == "assemble:no":
        await q.edit_message_text("Сборка отменена.")
        return
    await _submit_job(update, context, project, "assemble_force", lambda progress: assemble_work(app_settings(context), project, allow_unapproved=True, progress=progress), success_handler=_send_assembly_result)


async def check_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    project = await require_project(update, context)
    if not project:
        return
    try:
        files = await asyncio.to_thread(list_docx_files, app_settings(context), project)
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось получить DOCX: {exc}", reply_markup=MENU)
        return
    if not files:
        await update.effective_message.reply_text("DOCX-файлов пока нет.", reply_markup=MENU)
        return
    context.user_data["check_choices"] = [str(x.relative_to(get_project(app_settings(context), project))) for x in files[:60]]
    rows = [[InlineKeyboardButton(x.name[:55], callback_data=f"checkdoc:{i}")] for i, x in enumerate(files[:60])]
    await update.effective_message.reply_text("Какой DOCX проверить?", reply_markup=InlineKeyboardMarkup(rows))


async def checkdoc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    q = update.callback_query
    await q.answer()
    project = await require_project(update, context)
    try:
        idx = int(q.data.split(":", 1)[1])
        rel = context.user_data.get("check_choices", [])[idx]
    except Exception:
        await q.edit_message_text("Список устарел.")
        return
    await q.edit_message_text(f"Проверяю {Path(rel).name}…")
    try:
        report, path = await asyncio.to_thread(check_formatting, app_settings(context), project, rel)
        await q.message.reply_text(report[:3500], reply_markup=MENU)
        with path.open("rb") as fh:
            await q.message.reply_document(fh, filename=path.name)
    except Exception as exc:
        await q.message.reply_text(f"Ошибка проверки: {exc}", reply_markup=MENU)


async def diagnostics_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    text = await asyncio.to_thread(run_diagnostics, app_settings(context))
    await update.effective_message.reply_text(text[:4000], reply_markup=MENU)


# ---------- Text state machine ----------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update, context):
        return
    message = update.effective_message
    text = (message.text or "").strip()
    mode = context.user_data.get("mode")

    buttons = {
        BTN_NEW: new_work,
        BTN_SELECT: choose_work,
        BTN_PROJECT: project_button,
        BTN_MATERIALS: materials_button,
        BTN_PLAN: plan_button,
        BTN_GENERATE: generate_button,
        BTN_FEEDBACK: feedback_button,
        BTN_ASSEMBLE: assemble_button,
        BTN_CHECK: check_button,
        BTN_DIAG: diagnostics_button,
        BTN_CANCEL: cancel,
    }
    if text in buttons:
        reset_mode(context)
        await buttons[text](update, context)
        return

    if mode == "new_project_name":
        settings = app_settings(context)
        user = uid(update)
        try:
            await asyncio.to_thread(billing.check_project_quota, settings, user)
        except billing.PaymentRequired as exc:
            from notify import payment_request
            payment_request(
                settings, user,
                update.effective_user.username if update.effective_user else None,
                update.effective_user.first_name if update.effective_user else None,
                exc.used, exc.limit,
            )
            await message.reply_text(
                f"💳 Бесплатный лимит исчерпан: создано {exc.used} из {exc.limit} работ.\n"
                f"Напиши владельцу бота — после подтверждения оплаты доступ откроется автоматически.",
                reply_markup=MENU,
            )
            return
        try:
            project = await asyncio.to_thread(create_project, settings, text, str(context.user_data.get("new_work_type") or "other"), user)
            persist_selected(update, context, project.name)
            reset_mode(context)
            await message.reply_text(f"✅ Создана «{project.name}».\nТеперь отправь материалы прямо сюда.", reply_markup=MENU)
        except Exception as exc:
            await message.reply_text(f"Не удалось создать работу: {exc}", reply_markup=MENU)
        return

    if mode == "project_field":
        project = await require_project(update, context)
        field = str(context.user_data.get("project_field_name") or "")
        try:
            await asyncio.to_thread(update_project_field, app_settings(context), project, field, text)
            reset_mode(context)
            await message.reply_text("✅ Обновлено.", reply_markup=MENU)
        except Exception as exc:
            await message.reply_text(f"Ошибка: {exc}", reply_markup=MENU)
        return

    if mode == "edit_plan_json":
        project = await require_project(update, context)
        try:
            data = json.loads(text)
            plan = WorkPlan.from_dict(data)
            if not plan.sections:
                raise ValueError("sections пуст")
            await asyncio.to_thread(save_work_plan, get_project(app_settings(context), project), plan)
            reset_mode(context)
            await message.reply_text("✅ План обновлён. Существующие версии разделов не удалены.", reply_markup=MENU)
        except Exception as exc:
            await message.reply_text(f"JSON плана не принят: {exc}")
        return

    if mode == "generate_section_instruction":
        project = await require_project(update, context)
        sid = str(context.user_data.get("selected_section_id") or "")
        instruction = "" if text == "-" else text
        reset_mode(context)
        await _submit_job(update, context, project, "generate_section", lambda progress: generate_section(app_settings(context), project, sid, user_instruction=instruction, progress=progress), success_handler=_send_section_result)
        return

    if mode == "continue_section_instruction":
        project = await require_project(update, context)
        sid = str(context.user_data.get("selected_section_id") or "")
        reset_mode(context)
        await _submit_job(update, context, project, "continue_section", lambda progress: continue_section(app_settings(context), project, sid, text, progress=progress), success_handler=_send_section_result)
        return

    if mode == "feedback_text":
        project = await require_project(update, context)
        sid = str(context.user_data.get("feedback_section_id") or "")
        reset_mode(context)
        await _submit_job(update, context, project, "feedback_revision", lambda progress: revise_section(app_settings(context), project, sid, text, progress=progress), success_handler=_send_section_result)
        return

    if mode == "standalone_task":
        project = await require_project(update, context)
        reset_mode(context)
        await _submit_job(update, context, project, "standalone_task", lambda progress: _legacy_solve_with_progress(app_settings(context), project, text, progress), success_handler=_send_legacy_result)
        return

    if mode == "upload_materials":
        await message.reply_text("Отправь файл или фотографию. Для выхода нажми кнопку меню.", reply_markup=MENU)
        return

    await message.reply_text("Выбери действие кнопкой меню.", reply_markup=MENU)


# ---------- Jobs / result senders ----------

async def _submit_job(update: Update, context: ContextTypes.DEFAULT_TYPE, project: str, kind: str, func, *, success_handler, assembly_retry: bool = False) -> None:
    user = uid(update)
    message = update.effective_message or (update.callback_query.message if update.callback_query else None)
    if user is None or message is None:
        return
    manager = job_manager(context)
    try:
        job_id = manager.submit(user_id=user, project=project, kind=kind, func=func)
    except ProjectBusyError as exc:
        await message.reply_text(f"⏳ {exc}", reply_markup=MENU)
        return
    status_msg = await message.reply_text(f"⏳ Job {job_id}: в очереди\nМожно нажать «❌ Отменить».")
    future = manager.future(job_id)
    last = ""
    while future and not future.done():
        info = manager.info(job_id)
        if info and info.progress != last:
            last = info.progress
            try:
                await status_msg.edit_text(f"⏳ {kind}\n{info.progress}\nJob: {job_id}\nМожно нажать «❌ Отменить».")
            except Exception:
                pass
        await asyncio.sleep(1.5)
    try:
        result = await asyncio.wrap_future(future) if future else None
        await status_msg.edit_text(f"✅ {kind}: готово")
        await success_handler(message, result, context)
    except (JobCancelled, asyncio.CancelledError):
        await status_msg.edit_text("❌ Операция отменена.")
        await message.reply_text("Меню:", reply_markup=MENU)
    except AssemblyError as exc:
        await status_msg.edit_text(f"⚠️ Сборка остановлена:\n{str(exc)[:3200]}")
        if assembly_retry:
            await message.reply_text(
                "Собрать всё равно из доступных последних версий?",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Собрать", callback_data="assemble:yes"), InlineKeyboardButton("Вернуться", callback_data="assemble:no")]]),
            )
    except Exception as exc:
        logger.exception("Job failed: %s", kind)
        await status_msg.edit_text(f"❌ Ошибка: {str(exc)[:3400]}")
        await message.reply_text("Подробности — logs/agent.log.", reply_markup=MENU)


async def _send_plan_result(message, result, context):
    await message.reply_text(plan_text(result)[:4000], reply_markup=MENU)


async def _send_section_result(message, result, context):
    await message.reply_text(f"✅ {result.node.id} {result.node.title}\nВерсия: v{result.version}\nСтатус: {result.status}\nReview score: {result.score}/100", reply_markup=MENU)
    with result.docx_path.open("rb") as fh:
        await message.reply_document(fh, filename=result.docx_path.name)
    if result.warnings:
        await message.reply_text("Предупреждения:\n" + "\n".join(f"• {x}" for x in result.warnings[:12]))


async def _send_multi_section_result(message, result, context):
    await message.reply_text(f"✅ Обработка по разделам завершена. Новых/обновлённых разделов: {len(result)}", reply_markup=MENU)
    for item in result[-6:]:
        await message.reply_text(f"{item.node.id}: v{item.version}, {item.status}, score {item.score}")


async def _send_chapter_result(message, result, context):
    await message.reply_text("✅ Глава собрана из существующих разделов без повторной генерации.", reply_markup=MENU)
    with result.docx_path.open("rb") as fh:
        await message.reply_document(fh, filename=result.docx_path.name)
    if result.warnings:
        await message.reply_text("\n".join(f"• {x}" for x in result.warnings[:12]))


async def _send_assembly_result(message, result, context):
    await message.reply_text(f"✅ Финальная работа собрана. Использовано разделов: {len(result.included)}", reply_markup=MENU)
    for path in (result.docx_path, result.report_path):
        with path.open("rb") as fh:
            await message.reply_document(fh, filename=path.name)
    if result.warnings:
        await message.reply_text("Предупреждения:\n" + "\n".join(f"• {x}" for x in result.warnings[:12]))


async def _send_legacy_result(message, result, context):
    preview = result.answer if len(result.answer) <= 3200 else result.answer[:3200] + "\n…"
    await message.reply_text(preview, reply_markup=MENU)
    for path in (result.docx_path, result.txt_path):
        with path.open("rb") as fh:
            await message.reply_document(fh, filename=path.name)
    for graph in result.graph_paths[:5]:
        with graph.open("rb") as fh:
            await message.reply_photo(fh, caption=graph.stem)


def _legacy_solve_with_progress(settings, project, text, progress):
    progress("Читаю материалы и решаю отдельное задание…")
    return solve_task(settings, project, text)


async def _project_panel_text(context: ContextTypes.DEFAULT_TYPE, project: str) -> str:
    settings = app_settings(context)
    base = await asyncio.to_thread(project_summary, settings, project)
    project_path = get_project(settings, project)
    plan = await asyncio.to_thread(load_work_plan, project_path)
    req = await asyncio.to_thread(load_requirements, project_path)
    blocks = [base, "", await asyncio.to_thread(readiness_text, settings, project)]
    if plan:
        done, total, pct = plan.progress()
        blocks += ["", f"🗺 План: {pct}% ({done}/{total} approved/completed)"]
    else:
        blocks += ["", "🗺 План: не создан"]
    blocks += ["📘 Требования: проанализированы" if req else "📘 Требования: ещё не проанализированы"]
    return "\n".join(blocks)[:4000]


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Необработанная ошибка Telegram: %s", context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Произошла внутренняя ошибка. Подробности в logs/agent.log.", reply_markup=MENU)
        except Exception:
            pass


def build_application() -> Application:
    settings = load_settings(require_telegram=True)
    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(8)
        .post_init(bot_ext.post_init)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["jobs"] = JobManager(workers=2)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("id", show_id))

    app.add_handler(CallbackQueryHandler(new_type_callback, pattern=r"^newtype:[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(folder_callback, pattern=r"^folder:\d+$"))
    app.add_handler(CallbackQueryHandler(project_field_callback, pattern=r"^projectfield:(topic|variant)$"))
    app.add_handler(CallbackQueryHandler(requirements_callback, pattern=r"^requirements:analyze$"))
    app.add_handler(CallbackQueryHandler(project_sections_callback, pattern=r"^project:sections$"))
    app.add_handler(CallbackQueryHandler(section_view_callback, pattern=r"^sectionview:\d+$"))
    app.add_handler(CallbackQueryHandler(section_action_callback, pattern=r"^section:(download|versions|approve|continue|feedback|create)$"))
    app.add_handler(CallbackQueryHandler(section_version_callback, pattern=r"^sectionver:\d+$"))

    app.add_handler(CallbackQueryHandler(materials_callback, pattern=r"^materials:(add|list|roles|delete)$"))
    app.add_handler(CallbackQueryHandler(material_role_file_callback, pattern=r"^matrole:\d+$"))
    app.add_handler(CallbackQueryHandler(set_role_callback, pattern=r"^setrole:[a-z_]+$"))
    app.add_handler(CallbackQueryHandler(material_delete_callback, pattern=r"^matdel:\d+$"))
    app.add_handler(CallbackQueryHandler(delete_confirm_callback, pattern=r"^del:(yes|no)$"))

    app.add_handler(CallbackQueryHandler(plan_callback, pattern=r"^plan:(create|rebuild|edit|download)$"))
    app.add_handler(CallbackQueryHandler(gen_callback, pattern=r"^gen:(all|section|chapter|continue|task)$"))
    app.add_handler(CallbackQueryHandler(gen_section_callback, pattern=r"^gensec:\d+$"))
    app.add_handler(CallbackQueryHandler(continue_section_callback, pattern=r"^contsec:\d+$"))
    app.add_handler(CallbackQueryHandler(chapter_callback, pattern=r"^genchap:\d+$"))
    app.add_handler(CallbackQueryHandler(feedback_section_callback, pattern=r"^feedbacksec:\d+$"))
    app.add_handler(CallbackQueryHandler(force_assemble_callback, pattern=r"^assemble:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(checkdoc_callback, pattern=r"^checkdoc:\d+$"))

    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    bot_ext.attach(app)
    return app


def main() -> None:
    try:
        app = build_application()
        settings = app.bot_data["settings"]
        from webserver import start_webserver
        start_webserver(settings, app.bot_data["jobs"])
        if settings.admin_ids:
            logger.info("Уведомления владельцу активны: %s", sorted(settings.admin_ids))
        else:
            logger.warning("ADMIN_IDS пуст — пуш-уведомления владельцу и админ-команды не работают.")
        logger.info("Telegram-бот V3 запущен")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    except (ConfigError, ProjectError, AgentError, PipelineError) as exc:
        logger.error("Не удалось запустить бота: %s", exc)
        print(f"ОШИБКА: {exc}")
    except KeyboardInterrupt:
        print("Бот остановлен.")
    except Exception as exc:
        logger.exception("Критическая ошибка запуска бота")
        print(f"НЕОЖИДАННАЯ ОШИБКА: {exc}")


if __name__ == "__main__":
    main()
