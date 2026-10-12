from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from projects import get_project, list_materials
from readers import IMAGE_EXTENSIONS, TEXT_EXTENSIONS, ReaderError, SourceDocument, read_source
from settings import Settings
from vision import VisionError, analyze_image, analyze_pdf_scan


@dataclass(slots=True)
class IngestResult:
    documents: list[SourceDocument]
    warnings: list[str] = field(default_factory=list)
    vision_used: list[str] = field(default_factory=list)


def ingest_project(settings: Settings, project_name: str, *, allow_vision: bool = True) -> IngestResult:
    project = get_project(settings, project_name)
    materials = list_materials(settings, project_name)
    docs: list[SourceDocument] = []
    warnings: list[str] = []
    vision_used: list[str] = []

    for item in materials:
        rel = str(item.get("path") or "")
        path = (project / rel).resolve()
        if not path.exists():
            warnings.append(f"{rel}: файл не найден")
            continue
        ext = path.suffix.lower()
        if ext in TEXT_EXTENSIONS:
            # Текст извлекается один раз и кешируется по fingerprint (путь+размер+mtime),
            # как уже сделано для vision. Это убирает повторное чтение всех материалов
            # при генерации каждого раздела.
            cached = _read_cached(project, path, "text")
            if cached is not None:
                doc = SourceDocument(path=path, text=cached)
            else:
                try:
                    doc = read_source(path)
                except ReaderError as exc:
                    warnings.append(str(exc))
                    continue
                _write_cached(project, path, "text", doc.text)
            # Для PDF-скана используем vision только если локальное извлечение практически пустое.
            low_text = ext == ".pdf" and len("".join(doc.text.split())) < 80
            if low_text and allow_vision and settings.vision_ready:
                cached = _read_cached(project, path, "pdfvision")
                if cached is None:
                    try:
                        vision_text, vw = analyze_pdf_scan(settings, path)
                        warnings.extend(f"{path.name}: {x}" for x in vw)
                        if vision_text.strip():
                            _write_cached(project, path, "pdfvision", vision_text)
                            cached = vision_text
                            vision_used.append(path.name)
                    except VisionError as exc:
                        warnings.append(f"{path.name}: {exc}")
                if cached:
                    doc = SourceDocument(path=path, text=cached, warnings=doc.warnings)
            docs.append(doc)
            warnings.extend(f"{path.name}: {x}" for x in doc.warnings)
            continue

        if ext in IMAGE_EXTENSIONS:
            if not allow_vision or not settings.vision_ready:
                warnings.append(f"{path.name}: изображение сохранено, но Vision не настроен")
                continue
            cached = _read_cached(project, path, "vision")
            if cached is None:
                try:
                    cached = analyze_image(settings, path)
                    _write_cached(project, path, "vision", cached)
                    vision_used.append(path.name)
                except VisionError as exc:
                    warnings.append(f"{path.name}: {exc}")
                    continue
            docs.append(SourceDocument(path=path, text=cached))

    if not docs:
        warnings.append("Нет материалов с извлечённым текстом.")
    return IngestResult(documents=docs, warnings=warnings, vision_used=vision_used)


def _cache_dir(project: Path) -> Path:
    path = project / "work" / "extracted"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:20]


def _cache_path(project: Path, path: Path, kind: str) -> Path:
    return _cache_dir(project) / f"{_fingerprint(path)}_{kind}.txt"


def _read_cached(project: Path, path: Path, kind: str) -> str | None:
    cache = _cache_path(project, path, kind)
    if not cache.exists():
        return None
    try:
        return cache.read_text(encoding="utf-8")
    except OSError:
        return None


def _write_cached(project: Path, path: Path, kind: str, text: str) -> None:
    cache = _cache_path(project, path, kind)
    cache.write_text(text, encoding="utf-8")
