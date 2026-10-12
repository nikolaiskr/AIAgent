from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.env"
DATA_DIR = APP_DIR / "data"
LOGS_DIR = APP_DIR / "logs"
DB_PATH = DATA_DIR / "aiagent.db"
LOG_PATH = LOGS_DIR / "agent.log"


class ConfigError(RuntimeError):
    """Понятная ошибка конфигурации."""


@dataclass(slots=True)
class Settings:
    projects_root: Path
    telegram_bot_token: str | None
    allowed_user_ids: set[int]
    max_upload_mb: int

    openai_api_key: str | None
    openai_base_url: str | None
    openai_model: str | None
    openai_vision_model: str | None

    deepseek_api_key: str | None
    deepseek_base_url: str
    deepseek_model: str | None

    max_context_chars: int = 120_000
    max_output_tokens: int = 12_000
    top_chunks: int = 30
    chunk_chars: int = 4_500
    chunk_overlap: int = 500

    max_review_loops: int = 2
    review_score_threshold: int = 85
    use_quality_review: bool = True
    use_natural_editor: bool = True
    enable_vision: bool = True
    max_vision_pages: int = 12

    admin_ids: set[int] = field(default_factory=set)
    miniapp_url: str | None = None
    miniapp_host: str = "127.0.0.1"
    miniapp_port: int = 8080
    dev_mode: bool = False
    free_projects: int = 2
    version_keep: int = 10
    notify_job_events: bool = True
    owner_link: str | None = None
    allowed_origins: tuple = ()  # разрешённые источники CORS (GitHub Pages и т.п.)

    default_font_name: str = "Times New Roman"
    default_font_size_pt: float = 14.0
    default_line_spacing: float = 1.5
    default_first_line_indent_cm: float = 1.25
    default_margin_left_cm: float = 3.0
    default_margin_right_cm: float = 1.5
    default_margin_top_cm: float = 2.0
    default_margin_bottom_cm: float = 2.0

    @property
    def openai_ready(self) -> bool:
        return bool(self.openai_api_key and self.openai_model)

    @property
    def deepseek_ready(self) -> bool:
        return bool(self.deepseek_api_key and self.deepseek_model)

    @property
    def any_ai_ready(self) -> bool:
        return self.openai_ready or self.deepseek_ready

    @property
    def vision_ready(self) -> bool:
        return bool(self.enable_vision and self.openai_api_key and (self.openai_vision_model or self.openai_model))


def _auto_projects_root() -> Path:
    home = Path.home()
    candidates = [
        home / "YandexDisk" / "Курсовые",
        home / "Yandex.Disk" / "Курсовые",
        home / "Яндекс.Диск" / "Курсовые",
        home / "Yandex Disk" / "Курсовые",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _parse_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for piece in raw.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            result.add(int(piece))
        except ValueError as exc:
            raise ConfigError(
                "TELEGRAM_ALLOWED_USER_IDS должен содержать числовые Telegram user_id через запятую."
            ) from exc
    return result


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on", "да"}


def load_settings(*, require_telegram: bool = False) -> Settings:
    """Читает config.env. AI-ключи не обязательны для локальных функций и Telegram-оболочки."""
    if CONFIG_PATH.exists():
        load_dotenv(CONFIG_PATH, override=False)

    explicit_root = (
        os.getenv("PROJECTS_ROOT")
        or os.getenv("PROEJCTS_ROOT")
        or ""
    ).strip()
    projects_root = Path(explicit_root).expanduser() if explicit_root else _auto_projects_root()

    telegram_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() or None
    if require_telegram and not telegram_token:
        raise ConfigError("В config.env не заполнен TELEGRAM_BOT_TOKEN.")

    try:
        max_upload_mb = max(1, min(int(os.getenv("MAX_UPLOAD_MB", "20")), 50))
        max_context_chars = max(20_000, int(os.getenv("MAX_CONTEXT_CHARS", "120000")))
        max_output_tokens = max(1_000, int(os.getenv("MAX_OUTPUT_TOKENS", "12000")))
        top_chunks = max(6, int(os.getenv("TOP_CHUNKS", "30")))
        chunk_chars = max(1_500, int(os.getenv("CHUNK_CHARS", "4500")))
        raw_overlap = int(os.getenv("CHUNK_OVERLAP", "500"))
        chunk_overlap = max(0, min(raw_overlap, chunk_chars // 2))
        max_review_loops = max(0, min(int(os.getenv("MAX_REVIEW_LOOPS", "2")), 4))
        review_score_threshold = max(50, min(int(os.getenv("REVIEW_SCORE_THRESHOLD", "85")), 100))
        max_vision_pages = max(1, min(int(os.getenv("MAX_VISION_PAGES", "12")), 40))
        admin_ids = _parse_ids(os.getenv("ADMIN_IDS", ""))
        miniapp_url = (os.getenv("MINIAPP_URL") or "").strip() or None
        miniapp_host = (os.getenv("MINIAPP_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        miniapp_port = max(1, min(int(os.getenv("MINIAPP_PORT", "8080")), 65535))
        free_projects = max(0, int(os.getenv("FREE_PROJECTS", "2")))
        version_keep = max(2, int(os.getenv("VERSION_KEEP", "10")))
        allowed_origins = tuple(
            x.strip() for x in os.getenv("ALLOWED_ORIGINS", "").split(",") if x.strip()
        )
    except ValueError as exc:
        raise ConfigError("Числовые настройки в config.env заполнены неверно.") from exc

    return Settings(
        projects_root=projects_root,
        telegram_bot_token=telegram_token,
        allowed_user_ids=_parse_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")),
        max_upload_mb=max_upload_mb,
        openai_api_key=(os.getenv("OPENAI_API_KEY") or "").strip() or None,
        openai_base_url=(os.getenv("OPENAI_BASE_URL") or "").strip() or None,
        openai_model=(os.getenv("OPENAI_MODEL") or "").strip() or None,
        openai_vision_model=(os.getenv("OPENAI_VISION_MODEL") or "").strip() or None,
        deepseek_api_key=(os.getenv("DEEPSEEK_API_KEY") or "").strip() or None,
        deepseek_base_url=(os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").strip(),
        deepseek_model=(os.getenv("DEEPSEEK_MODEL") or "").strip() or None,
        max_context_chars=max_context_chars,
        max_output_tokens=max_output_tokens,
        top_chunks=top_chunks,
        chunk_chars=chunk_chars,
        chunk_overlap=chunk_overlap,
        max_review_loops=max_review_loops,
        review_score_threshold=review_score_threshold,
        use_quality_review=_bool_env("USE_QUALITY_REVIEW", True),
        use_natural_editor=_bool_env("USE_NATURAL_EDITOR", True),
        enable_vision=_bool_env("ENABLE_VISION", True),
        max_vision_pages=max_vision_pages,
        admin_ids=admin_ids,
        miniapp_url=miniapp_url,
        miniapp_host=miniapp_host,
        miniapp_port=miniapp_port,
        dev_mode=_bool_env("DEV_MODE", False),
        free_projects=free_projects,
        version_keep=version_keep,
        notify_job_events=_bool_env("NOTIFY_JOB_EVENTS", True),
        owner_link=(os.getenv("OWNER_LINK") or "").strip() or None,
        allowed_origins=allowed_origins,
        default_font_name=(os.getenv("DEFAULT_FONT_NAME") or "Times New Roman").strip(),
        default_font_size_pt=float(os.getenv("DEFAULT_FONT_SIZE_PT", "14")),
        default_line_spacing=float(os.getenv("DEFAULT_LINE_SPACING", "1.5")),
        default_first_line_indent_cm=float(os.getenv("DEFAULT_FIRST_LINE_INDENT_CM", "1.25")),
        default_margin_left_cm=float(os.getenv("DEFAULT_MARGIN_LEFT_CM", "3")),
        default_margin_right_cm=float(os.getenv("DEFAULT_MARGIN_RIGHT_CM", "1.5")),
        default_margin_top_cm=float(os.getenv("DEFAULT_MARGIN_TOP_CM", "2")),
        default_margin_bottom_cm=float(os.getenv("DEFAULT_MARGIN_BOTTOM_CM", "2")),
    )
