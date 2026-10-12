from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from readers import IMAGE_EXTENSIONS, TEXT_EXTENSIONS, UPLOAD_EXTENSIONS, ReaderError, read_source
from settings import Settings


class ProjectError(RuntimeError):
    pass


WORK_TYPES: dict[str, str] = {
    "coursework": "📚 Курсовая",
    "thesis": "🎓 ВКР",
    "lab": "🧪 Лабораторная",
    "practical": "📝 Практическая",
    "task": "🧮 Отдельное задание",
    "other": "❓ Другое",
}

TYPE_SUFFIX = {
    "coursework": "Курсовая",
    "thesis": "ВКР",
    "lab": "Лабораторная",
    "practical": "Практическая",
    "task": "Задание",
    "other": "Работа",
}

ROLE_LABELS: dict[str, str] = {
    "methodology": "📘 Методичка",
    "assignment": "🎯 Тема / задание",
    "source_data": "📊 Исходные данные",
    "additional": "📎 Доп. материалы",
    "example": "📄 Пример работы",
    "literature": "📚 Литература / источники",
    "feedback": "💬 Замечания преподавателя",
    "current_version": "📝 Текущая версия работы",
    "image": "🖼 Изображение",
    "author_style_sample": "✍️ Пример авторского стиля",
    "other": "❓ Другое / не определено",
}

PROJECT_META = "project.json"
RESERVED_DIRS = {"output", "work", "sections", ".aiagent", "__pycache__"}

ROLE_DIRS = {
    "methodology": "methodology",
    "assignment": "assignment",
    "literature": "literature",
    "source_data": "source_data",
    "example": "examples",
    "feedback": "feedback",
    "current_version": "current_version",
    "author_style_sample": "author_style_sample",
    "additional": "additional",
    "image": "images",
    "other": "other",
}


def safe_name(value: str, fallback: str = "Работа", max_len: int = 90) -> str:
    value = value.strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    return value[:max_len]


def ensure_root(settings: Settings, *, create: bool = True) -> Path:
    root = settings.projects_root
    try:
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if not root.exists() or not root.is_dir():
            raise ProjectError(f"Папка Курсовые не найдена: {root}")
    except OSError as exc:
        raise ProjectError(f"Не удалось открыть/создать папку Курсовые: {root}\n{exc}") from exc
    return root


def _inside(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def list_projects(settings: Settings) -> list[Path]:
    root = ensure_root(settings)
    try:
        return sorted(
            [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")],
            key=lambda p: p.name.casefold(),
        )
    except OSError as exc:
        raise ProjectError(f"Не удалось получить список работ в {root}: {exc}") from exc


def get_project(settings: Settings, project_name: str) -> Path:
    root = ensure_root(settings).resolve()
    project = (root / project_name).resolve()
    if not _inside(root, project) or project == root:
        raise ProjectError("Недопустимый путь к работе.")
    if not project.exists() or not project.is_dir():
        raise ProjectError(f"Папка работы не найдена: {project}")
    return project


def infer_type_from_name(name: str) -> str:
    lower = name.casefold()
    if "вкр" in lower or "диплом" in lower:
        return "thesis"
    if "курсов" in lower:
        return "coursework"
    if "лаборатор" in lower or "лаб" in lower:
        return "lab"
    if "практич" in lower:
        return "practical"
    if "задан" in lower or "задач" in lower:
        return "task"
    return "other"


def make_project_name(base_name: str, work_type: str) -> str:
    base = safe_name(base_name, fallback="Новая работа", max_len=65)
    suffix = TYPE_SUFFIX.get(work_type, "Работа")
    if suffix.casefold() in base.casefold():
        return base
    return safe_name(f"{base}-{suffix}", max_len=90)


def _default_meta(folder_name: str, work_type: str) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "schema_version": 3,
        "name": folder_name,
        "type": work_type,
        "status": "collecting_materials",
        "topic": None,
        "variant": None,
        "created_at": now,
        "updated_at": now,
        "files": [],
        "owner_user_id": None,
        "requirements_status": "not_analyzed",
        "plan_status": "not_created",
    }


def _meta_path(project: Path) -> Path:
    return project / PROJECT_META


def ensure_project_structure(project: Path) -> None:
    for name in ("input", "sections", "work", "output"):
        (project / name).mkdir(exist_ok=True)
    input_dir = project / "input"
    for dirname in sorted(set(ROLE_DIRS.values())):
        (input_dir / dirname).mkdir(exist_ok=True)


def load_project_meta(settings: Settings, project_name: str) -> dict[str, Any]:
    project = get_project(settings, project_name)
    ensure_project_structure(project)
    path = _meta_path(project)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("project.json должен содержать объект")
        except Exception as exc:
            raise ProjectError(f"Не удалось прочитать {path}: {exc}") from exc
    else:
        data = _default_meta(project.name, infer_type_from_name(project.name))
    data.setdefault("schema_version", 3)
    data.setdefault("name", project.name)
    data.setdefault("type", infer_type_from_name(project.name))
    data.setdefault("topic", None)
    data.setdefault("variant", None)
    data.setdefault("files", [])
    data.setdefault("owner_user_id", None)
    data.setdefault("requirements_status", "not_analyzed")
    data.setdefault("plan_status", "not_created")
    _sync_files(project, data)
    save_project_meta(project, data)
    return data


def save_project_meta(project: Path, data: dict[str, Any]) -> None:
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path = _meta_path(project)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def create_project(settings: Settings, base_name: str, work_type: str = "other", owner_user_id: int | None = None) -> Path:
    root = ensure_root(settings)
    if work_type not in WORK_TYPES:
        work_type = "other"
    folder_name = make_project_name(base_name, work_type)
    project = (root / folder_name).resolve()
    if not _inside(root.resolve(), project):
        raise ProjectError("Недопустимое имя папки.")
    try:
        project.mkdir(parents=False, exist_ok=False)
        ensure_project_structure(project)
        meta = _default_meta(folder_name, work_type)
        meta["owner_user_id"] = int(owner_user_id) if owner_user_id is not None else None
        save_project_meta(project, meta)
    except FileExistsError:
        raise ProjectError(f"Работа «{folder_name}» уже существует.")
    except OSError as exc:
        raise ProjectError(f"Не удалось создать работу «{folder_name}»: {exc}") from exc
    return project


def _guess_role_from_filename(path: Path) -> str:
    name = path.name.casefold()
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    checks = [
        ("methodology", ("метод", "требован", "гост", "оформ", "указан", "рекомендац", "регламент")),
        ("feedback", ("замечан", "комментар", "правк", "реценз")),
        ("author_style_sample", ("мой стиль", "авторский стиль", "прошлая работа", "моя работа")),
        ("example", ("пример", "образец")),
        ("literature", ("литератур", "источник", "библиограф")),
        ("source_data", ("данн", "таблиц", "статист", "расчет", "расчёт", "вариант")),
        ("assignment", ("тем", "задан", "задач", "вариант")),
        ("current_version", ("курсов", "вкр", "диплом", "работа", "черновик", "версия")),
    ]
    for role, hints in checks:
        if any(h in name for h in hints):
            return role
    if ext == ".xlsx":
        return "source_data"
    return "other"


def _source_paths(project: Path) -> list[Path]:
    paths: list[Path] = []
    for path in project.rglob("*"):
        if not path.is_file():
            continue
        if path.name in {PROJECT_META} or path.name.startswith("~$") or path.name.startswith("."):
            continue
        if path.name.startswith(("Результат_", "Проверка_оформления_")):
            continue
        rel_parts = path.relative_to(project).parts
        if rel_parts and rel_parts[0] in RESERVED_DIRS:
            continue
        if path.suffix.lower() not in UPLOAD_EXTENSIONS:
            continue
        paths.append(path)
    return sorted(paths, key=lambda p: str(p).casefold())


def _sync_files(project: Path, meta: dict[str, Any]) -> None:
    old_entries = {
        str(item.get("path")): item
        for item in meta.get("files", [])
        if isinstance(item, dict) and item.get("path")
    }
    new_entries: list[dict[str, Any]] = []
    for path in _source_paths(project):
        rel = str(path.relative_to(project)).replace("\\", "/")
        old = old_entries.get(rel, {})
        guessed = _guess_role_from_filename(path)
        role = old.get("role") or guessed
        try:
            stat = path.stat()
            size = stat.st_size
            modified = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        except OSError:
            size = 0
            modified = None
        new_entries.append(
            {
                "path": rel,
                "name": path.name,
                "extension": path.suffix.lower(),
                "size": size,
                "modified_at": modified,
                "role": role,
                "guessed_role": guessed,
                "role_manual": bool(old.get("role_manual", False)),
            }
        )
    meta["files"] = new_entries
    meta["status"] = "materials_uploaded" if new_entries else "collecting_materials"


def list_materials(settings: Settings, project_name: str) -> list[dict[str, Any]]:
    meta = load_project_meta(settings, project_name)
    return list(meta.get("files", []))


def set_material_role(settings: Settings, project_name: str, relative_path: str, role: str) -> None:
    if role not in ROLE_LABELS:
        raise ProjectError("Неизвестная роль материала.")
    project = get_project(settings, project_name).resolve()
    meta = load_project_meta(settings, project_name)
    item = next((x for x in meta.get("files", []) if x.get("path") == relative_path), None)
    if item is None:
        raise ProjectError("Материал не найден. Обнови список материалов.")

    source = (project / relative_path).resolve()
    if not _inside(project, source) or not source.exists():
        raise ProjectError("Исходный файл не найден.")

    # V3 раскладывает материалы по role-папкам внутри input/. Старые файлы в корне проекта
    # переносятся только когда пользователь явно назначил им роль.
    role_dir = project / "input" / ROLE_DIRS.get(role, "other")
    role_dir.mkdir(parents=True, exist_ok=True)
    target = source if source.parent.resolve() == role_dir.resolve() else _dedupe_target(role_dir, source.name).resolve()
    if source != target:
        try:
            shutil.move(str(source), str(target))
        except OSError as exc:
            raise ProjectError(f"Не удалось переместить материал в папку роли: {exc}") from exc

    # Синхронизация после перемещения, затем сохраняем выбранную роль как ручную.
    meta = load_project_meta(settings, project_name)
    rel = str(target.relative_to(project)).replace("\\", "/")
    matched = False
    for row in meta.get("files", []):
        if row.get("path") == rel:
            row["role"] = role
            row["role_manual"] = True
            matched = True
            break
    if not matched:
        raise ProjectError("Файл перемещён, но не удалось обновить project.json.")
    save_project_meta(project, meta)


def _dedupe_target(directory: Path, filename: str) -> Path:
    filename = safe_name(filename, fallback="material")
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    for i in range(2, 1000):
        candidate = directory / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return directory / f"{stem}_{stamp}{suffix}"


def prepare_upload_target(settings: Settings, project_name: str, filename: str) -> Path:
    project = get_project(settings, project_name)
    ensure_project_structure(project)
    cleaned = safe_name(filename, fallback="material")
    ext = Path(cleaned).suffix.lower()
    if ext not in UPLOAD_EXTENSIONS:
        allowed = ", ".join(sorted(UPLOAD_EXTENSIONS))
        raise ProjectError(f"Формат {ext or '[без расширения]'} не разрешён. Поддерживаются: {allowed}")
    guessed_role = _guess_role_from_filename(Path(cleaned))
    role_dir = project / "input" / ROLE_DIRS.get(guessed_role, "other")
    role_dir.mkdir(parents=True, exist_ok=True)
    target = _dedupe_target(role_dir, cleaned).resolve()
    if not _inside((project / "input").resolve(), target):
        raise ProjectError("Недопустимое имя файла.")
    return target


def register_uploaded_file(settings: Settings, project_name: str, target: Path) -> dict[str, Any]:
    meta = load_project_meta(settings, project_name)
    # load_project_meta уже синхронизировал файл, если он существует.
    rel = str(target.relative_to(get_project(settings, project_name))).replace("\\", "/")
    for item in meta.get("files", []):
        if item.get("path") == rel:
            return item
    raise ProjectError("Файл сохранён, но не удалось зарегистрировать его в project.json.")


def delete_material(settings: Settings, project_name: str, relative_path: str) -> None:
    project = get_project(settings, project_name).resolve()
    target = (project / relative_path).resolve()
    if not _inside(project, target):
        raise ProjectError("Недопустимый путь к материалу.")
    if not target.exists() or not target.is_file():
        raise ProjectError("Материал уже удалён или не найден.")
    if target.name == PROJECT_META:
        raise ProjectError("project.json нельзя удалять как материал.")
    try:
        target.unlink()
    except OSError as exc:
        raise ProjectError(f"Не удалось удалить файл: {exc}") from exc
    meta = load_project_meta(settings, project_name)
    save_project_meta(project, meta)


def read_materials(settings: Settings, project_name: str):
    project = get_project(settings, project_name)
    documents = []
    warnings: list[str] = []
    for path in _source_paths(project):
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            doc = read_source(path)
            documents.append(doc)
            warnings.extend(f"{path.name}: {w}" for w in doc.warnings)
        except ReaderError as exc:
            warnings.append(str(exc))
    if not documents:
        warnings.append("В работе нет читаемых текстовых PDF/DOCX/TXT/XLSX/MD материалов.")
    return documents, warnings


def project_summary(settings: Settings, project_name: str) -> str:
    meta = load_project_meta(settings, project_name)
    files = list(meta.get("files", []))
    counts = {"pdf": 0, "docx": 0, "xlsx": 0, "text": 0, "image": 0}
    roles: dict[str, int] = {}
    for item in files:
        ext = str(item.get("extension") or "")
        if ext == ".pdf": counts["pdf"] += 1
        elif ext == ".docx": counts["docx"] += 1
        elif ext == ".xlsx": counts["xlsx"] += 1
        elif ext in {".txt", ".md"}: counts["text"] += 1
        elif ext in IMAGE_EXTENSIONS: counts["image"] += 1
        role = str(item.get("role") or "other")
        roles[role] = roles.get(role, 0) + 1

    work_type = WORK_TYPES.get(str(meta.get("type")), WORK_TYPES["other"])
    status = "🔵 Материалы загружены" if files else "🟡 Ждём материалы"
    topic = meta.get("topic") or "не указана"
    variant = meta.get("variant") or "не указан / не требуется"

    role_lines = []
    for role, count in sorted(roles.items(), key=lambda x: (-x[1], x[0])):
        role_lines.append(f"• {ROLE_LABELS.get(role, role)}: {count}")

    return "\n".join([
        f"📚 Работа: {meta.get('name', project_name)}",
        f"Тип: {work_type}",
        f"Статус: {status}",
        f"Тема: {topic}",
        f"Вариант: {variant}",
        "",
        f"Материалов: {len(files)}",
        f"📄 PDF: {counts['pdf']}",
        f"📝 DOCX: {counts['docx']}",
        f"📊 XLSX: {counts['xlsx']}",
        f"📃 TXT/MD: {counts['text']}",
        f"🖼 Изображения: {counts['image']}",
        "",
        "Роли материалов:",
        *(role_lines or ["• пока нет материалов"]),
    ])


def materials_text(settings: Settings, project_name: str, limit: int = 50) -> str:
    files = list_materials(settings, project_name)
    if not files:
        return "В этой работе пока нет материалов. Отправь PDF/DOCX/XLSX/TXT/MD или фотографию в чат."
    lines = [f"📎 Материалы «{project_name}» — {len(files)} шт.", ""]
    for i, item in enumerate(files[:limit], start=1):
        role = ROLE_LABELS.get(str(item.get("role") or "other"), "❓ Другое")
        size_kb = int(item.get("size") or 0) / 1024
        manual = " (вручную)" if item.get("role_manual") else ""
        lines.append(f"{i}. {item.get('name')} — {role}{manual}, {size_kb:.0f} КБ")
    if len(files) > limit:
        lines.append(f"…ещё {len(files) - limit} файлов")
    return "\n".join(lines)


def readiness_text(settings: Settings, project_name: str) -> str:
    meta = load_project_meta(settings, project_name)
    files = list(meta.get("files", []))
    roles = {str(item.get("role") or "other") for item in files}
    work_type = str(meta.get("type") or "other")
    lines = [f"Готовность «{project_name}»:" ]
    lines.append("✅ Материалы загружены" if files else "❌ Материалы не загружены")

    if work_type in {"coursework", "thesis"}:
        lines.append("✅ Тема/задание предположительно найдено" if "assignment" in roles else "⚠️ Тема/задание не отмечено")
        lines.append("✅ Методичка предположительно найдена" if "methodology" in roles else "⚠️ Методичка не отмечена")
    elif work_type in {"lab", "practical", "task"}:
        lines.append("✅ Задание предположительно найдено" if "assignment" in roles else "⚠️ Задание не отмечено")

    images = sum(1 for item in files if str(item.get("extension")) in IMAGE_EXTENSIONS)
    if images:
        lines.append(f"⚠️ Изображений: {images}. Они сохранены, но до подключения vision их содержимое не анализируется.")
    lines.append("\nВажно: до подключения AI это только локальная проверка наличия материалов и их ролей.")
    return "\n".join(lines)


def update_project_field(settings: Settings, project_name: str, field_name: str, value: str | None) -> None:
    if field_name not in {"topic", "variant"}:
        raise ProjectError("Это поле нельзя изменять через интерфейс.")
    project = get_project(settings, project_name)
    meta = load_project_meta(settings, project_name)
    cleaned = (value or "").strip()
    if cleaned.casefold() in {"нет", "не требуется", "-", "none"}:
        cleaned = ""
    meta[field_name] = cleaned or None
    save_project_meta(project, meta)


def ensure_project_owner(settings: Settings, project_name: str, user_id: int, *, claim_if_empty: bool = True) -> None:
    """Проверяет ownership. Старый проект без owner можно закрепить за первым пользователем."""
    project = get_project(settings, project_name)
    meta = load_project_meta(settings, project_name)
    owner = meta.get("owner_user_id")
    if owner in (None, "") and claim_if_empty:
        meta["owner_user_id"] = int(user_id)
        save_project_meta(project, meta)
        return
    if owner not in (None, "") and int(owner) != int(user_id):
        raise ProjectError("Эта работа принадлежит другому пользователю.")


def list_projects_for_user(settings: Settings, user_id: int) -> list[Path]:
    result: list[Path] = []
    for project in list_projects(settings):
        try:
            meta = load_project_meta(settings, project.name)
        except Exception:
            continue
        owner = meta.get("owner_user_id")
        if owner in (None, "") or int(owner) == int(user_id):
            result.append(project)
    return result
