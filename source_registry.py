from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from projects import get_project, list_materials
from readers import SourceDocument
from settings import Settings


class SourceRegistryError(RuntimeError):
    pass


@dataclass(slots=True)
class SourceRecord:
    id: str
    filename: str
    relative_path: str
    author: str | None = None
    title: str | None = None
    year: int | None = None
    role: str = "literature"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


REGISTRY_FILE = "sources.json"
SOURCE_RE = re.compile(r"\[(SOURCE_\d{3,})\]", re.IGNORECASE)


def registry_path(project: Path) -> Path:
    return project / REGISTRY_FILE


def load_registry(project: Path) -> list[SourceRecord]:
    path = registry_path(project)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("sources", []) if isinstance(raw, dict) else []
        result: list[SourceRecord] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            result.append(SourceRecord(**{k: item.get(k) for k in SourceRecord.__dataclass_fields__.keys()}))
        return result
    except Exception as exc:
        raise SourceRegistryError(f"Не удалось прочитать sources.json: {exc}") from exc


def save_registry(project: Path, rows: Sequence[SourceRecord]) -> Path:
    path = registry_path(project)
    data = {
        "schema_version": 1,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": [x.to_dict() for x in rows],
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def build_registry(settings: Settings, project_name: str, documents: Sequence[SourceDocument]) -> list[SourceRecord]:
    project = get_project(settings, project_name)
    materials = list_materials(settings, project_name)
    roles = {str(x.get("path")): str(x.get("role")) for x in materials}
    existing = {x.relative_path: x for x in load_registry(project)}
    docs_by_rel: dict[str, SourceDocument] = {}
    for doc in documents:
        try:
            rel = str(doc.path.relative_to(project)).replace("\\", "/")
        except ValueError:
            rel = doc.path.name
        docs_by_rel[rel] = doc

    candidates: list[str] = []
    for item in materials:
        role = str(item.get("role") or "other")
        if role in {"literature", "additional"}:
            candidates.append(str(item.get("path")))

    rows: list[SourceRecord] = []
    next_num = 1
    used_ids: set[str] = set()
    for rel in candidates:
        old = existing.get(rel)
        if old:
            rows.append(old)
            used_ids.add(old.id.upper())
    while f"SOURCE_{next_num:03d}" in used_ids:
        next_num += 1

    known = {x.relative_path for x in rows}
    for rel in candidates:
        if rel in known:
            continue
        doc = docs_by_rel.get(rel)
        filename = Path(rel).name
        author, title, year = _infer_metadata(filename, doc.text if doc else "")
        sid = f"SOURCE_{next_num:03d}"
        next_num += 1
        rows.append(
            SourceRecord(
                id=sid,
                filename=filename,
                relative_path=rel,
                author=author,
                title=title,
                year=year,
                role=roles.get(rel, "literature"),
                created_at=datetime.now().isoformat(timespec="seconds"),
            )
        )
    save_registry(project, rows)
    return rows


def source_context(rows: Sequence[SourceRecord]) -> str:
    if not rows:
        return "[Зарегистрированных источников нет.]"
    blocks = []
    for row in rows:
        meta = [row.id, f"файл={row.filename}"]
        if row.author:
            meta.append(f"автор={row.author}")
        if row.title:
            meta.append(f"название={row.title}")
        if row.year:
            meta.append(f"год={row.year}")
        blocks.append(" | ".join(meta))
    return "\n".join(blocks)


def validate_citations(text: str, rows: Sequence[SourceRecord]) -> tuple[bool, list[str]]:
    allowed = {x.id.upper() for x in rows}
    used = {m.group(1).upper() for m in SOURCE_RE.finditer(text)}
    issues: list[str] = []
    for sid in sorted(used - allowed):
        issues.append(f"Ссылка {sid} отсутствует в SourceRegistry.")
    # Паттерны, похожие на искусственно сгенерированную литературу без registry ID.
    suspicious = re.findall(r"(?:Иванов|Петров|Сидоров)\s+[А-Я]\.[А-Я]\.\s*[,–—-]", text)
    if suspicious and not used:
        issues.append("В тексте есть библиографически похожие конструкции без SOURCE_ID; проверь, не придуманы ли источники.")
    return not issues, issues


def citation_number_map(rows: Sequence[SourceRecord]) -> dict[str, int]:
    return {row.id.upper(): i for i, row in enumerate(rows, start=1)}


def bibliography_text(rows: Sequence[SourceRecord]) -> str:
    """Не выдумывает недостающие metadata: fallback — имя загруженного файла."""
    lines: list[str] = []
    for i, row in enumerate(rows, start=1):
        parts = []
        if row.author:
            parts.append(row.author)
        if row.title:
            parts.append(row.title)
        else:
            parts.append(row.filename)
        if row.year:
            parts.append(str(row.year))
        lines.append(f"{i}. " + ". — ".join(parts))
    return "\n".join(lines)


def _infer_metadata(filename: str, text: str) -> tuple[str | None, str | None, int | None]:
    sample = " ".join(text.split())[:1800]
    year = None
    years = [int(x) for x in re.findall(r"\b(?:19|20)\d{2}\b", sample)]
    if years:
        year = max(y for y in years if 1900 <= y <= 2100)
    # Только консервативная эвристика. Не назначаем автора, если формат не похож на Фамилия И.О.
    author = None
    m = re.search(r"\b([А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)\b", sample)
    if m:
        author = m.group(1)
    title = None
    stem = Path(filename).stem.replace("_", " ").strip()
    if len(stem) >= 5 and not re.fullmatch(r"(?:document|scan|img|file|документ)[\s\d()-]*", stem, flags=re.I):
        title = stem[:240]
    return author, title, year
