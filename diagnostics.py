from __future__ import annotations

import importlib.metadata
import platform
import sys
from pathlib import Path

from projects import ensure_root
from settings import CONFIG_PATH, DB_PATH, LOG_PATH, Settings


def _pkg_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return "не найден"


def run_diagnostics(settings: Settings) -> str:
    lines: list[str] = ["⚙️ Диагностика AI Agent V3", ""]
    lines.append(f"{'✅' if CONFIG_PATH.exists() else '⚠️'} config.env: {CONFIG_PATH}")
    lines.append(f"{'✅' if settings.telegram_bot_token else '❌'} Telegram token: {'настроен' if settings.telegram_bot_token else 'не настроен'}")
    lines.append(f"{'✅' if settings.allowed_user_ids else '⚠️'} Access whitelist: {len(settings.allowed_user_ids) if settings.allowed_user_ids else 'выключен'}")

    root = settings.projects_root
    lines += ["", f"Папка проектов: {root}"]
    try:
        ensure_root(settings)
        lines.append("✅ Корень доступен")
        probe = root / ".aiagent_write_test.tmp"
        probe.write_text("ok", encoding="utf-8")
        if probe.read_text(encoding="utf-8") == "ok":
            lines.append("✅ Чтение/запись работает")
        probe.unlink(missing_ok=True)
        lines.append("✅ Удаление работает")
    except Exception as exc:
        lines.append(f"❌ Доступ к папке: {exc}")

    soul = root / "soul.md"
    lines.append(f"{'✅' if soul.exists() else '⚠️'} soul.md: {'найден' if soul.exists() else 'не найден'}")

    lines += ["", "AI / hybrid:"]
    lines.append(f"{'✅' if settings.deepseek_ready else '⚪'} DeepSeek writer: {'готов' if settings.deepseek_ready else 'не настроен'}")
    lines.append(f"{'✅' if settings.openai_ready else '⚪'} OpenAI quality: {'готов' if settings.openai_ready else 'не настроен'}")
    lines.append(f"{'✅' if settings.vision_ready else '⚪'} OpenAI vision: {'готов' if settings.vision_ready else 'не настроен'}")
    if not settings.any_ai_ready:
        lines.append("ℹ️ Без AI работают проекты, загрузка, роли, plan fallback, version storage, DOCX checks и диагностика.")

    lines += ["", "Pipeline:"]
    lines.append(f"MAX_REVIEW_LOOPS={settings.max_review_loops}")
    lines.append(f"REVIEW_SCORE_THRESHOLD={settings.review_score_threshold}")
    lines.append(f"MAX_CONTEXT_CHARS={settings.max_context_chars}")
    lines.append(f"MAX_VISION_PAGES={settings.max_vision_pages}")

    lines += ["", "Mini App / биллинг:"]
    lines.append(f"{'✅' if settings.admin_ids else '⚠️'} ADMIN_IDS: {len(settings.admin_ids) if settings.admin_ids else 'не заданы — пуши и админка не работают'}")
    lines.append(f"{'✅' if settings.miniapp_url else '⚠️'} MINIAPP_URL: {settings.miniapp_url or 'не задан — кнопка приложения не показывается'}")
    lines.append(f"Сервер приложения: http://{settings.miniapp_host}:{settings.miniapp_port}")
    lines.append(f"Бесплатных работ: {settings.free_projects}")
    lines.append(f"Хранить версий раздела: {settings.version_keep}")
    lines.append(f"Пуши о job'ах: {'вкл' if settings.notify_job_events else 'выкл'}")
    try:
        import aiohttp
        lines.append(f"✅ aiohttp: {importlib.metadata.version('aiohttp')}")
    except ImportError:
        lines.append("❌ aiohttp: не установлен — Mini App сервер не поднимется (pip install aiohttp)")

    lines += ["", "Окружение:", f"Python: {sys.version.split()[0]} ({platform.system()})"]
    packages = [
        "python-telegram-bot", "python-docx", "PyPDF2", "pandas", "openpyxl",
        "matplotlib", "numpy", "sympy", "openai", "python-dotenv", "PyMuPDF",
    ]
    for package in packages:
        version = _pkg_version(package)
        lines.append(f"{'✅' if version != 'не найден' else '❌'} {package}: {version}")

    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines.append(f"✅ SQLite state/jobs: {DB_PATH}")
    except Exception as exc:
        lines.append(f"❌ SQLite: {exc}")
    lines.append(f"Log: {LOG_PATH}")
    lines.append(f"Макс. upload: {settings.max_upload_mb} МБ")
    return "\n".join(lines)
