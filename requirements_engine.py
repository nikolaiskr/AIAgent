from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from academic_renderer import FormattingProfile
from projects import get_project, list_materials
from providers.router import HybridRouter
from readers import SourceDocument
from settings import Settings


class RequirementError(RuntimeError):
    pass


@dataclass(slots=True)
class WorkRequirements:
    work_type: str | None = None
    pages_min: int | None = None
    pages_max: int | None = None
    chapters: int | None = None
    sections_per_chapter_min: int | None = None
    sources_min: int | None = None
    source_recency_years: int | None = None
    introduction_requirements: list[str] = field(default_factory=list)
    conclusion_requirements: list[str] = field(default_factory=list)
    tables_required: bool | None = None
    figures_required: bool | None = None
    calculations_required: bool | None = None
    applications_required: bool | None = None
    formatting: FormattingProfile = field(default_factory=FormattingProfile)
    notes: list[str] = field(default_factory=list)
    confidence: str = "partial"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["formatting"] = self.formatting.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkRequirements":
        fmt = data.get("formatting") if isinstance(data.get("formatting"), dict) else {}
        return cls(
            work_type=data.get("work_type"),
            pages_min=_int_or_none(data.get("pages_min")),
            pages_max=_int_or_none(data.get("pages_max")),
            chapters=_int_or_none(data.get("chapters")),
            sections_per_chapter_min=_int_or_none(data.get("sections_per_chapter_min")),
            sources_min=_int_or_none(data.get("sources_min")),
            source_recency_years=_int_or_none(data.get("source_recency_years")),
            introduction_requirements=_str_list(data.get("introduction_requirements")),
            conclusion_requirements=_str_list(data.get("conclusion_requirements")),
            tables_required=_bool_or_none(data.get("tables_required")),
            figures_required=_bool_or_none(data.get("figures_required")),
            calculations_required=_bool_or_none(data.get("calculations_required")),
            applications_required=_bool_or_none(data.get("applications_required")),
            formatting=FormattingProfile.from_dict(fmt),
            notes=_str_list(data.get("notes")),
            confidence=str(data.get("confidence") or "partial"),
        )


def requirements_path(project: Path) -> Path:
    return project / "requirements.json"


def load_requirements(project: Path) -> WorkRequirements | None:
    path = requirements_path(project)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return WorkRequirements.from_dict(data if isinstance(data, dict) else {})
    except Exception as exc:
        raise RequirementError(f"Не удалось прочитать requirements.json: {exc}") from exc


def save_requirements(project: Path, req: WorkRequirements) -> Path:
    path = requirements_path(project)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(req.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def methodology_documents(settings: Settings, project_name: str, documents: Sequence[SourceDocument]) -> list[SourceDocument]:
    roles = {str(x.get("path")): str(x.get("role")) for x in list_materials(settings, project_name)}
    result = []
    for doc in documents:
        project = get_project(settings, project_name)
        try:
            rel = str(doc.path.relative_to(project)).replace("\\", "/")
        except ValueError:
            rel = doc.path.name
        if roles.get(rel) == "methodology" or "метод" in doc.path.name.casefold() or "требован" in doc.path.name.casefold():
            result.append(doc)
    return result


def analyze_requirements(
    settings: Settings,
    project_name: str,
    documents: Sequence[SourceDocument],
    *,
    use_ai: bool = True,
) -> WorkRequirements:
    project = get_project(settings, project_name)
    docs = methodology_documents(settings, project_name, documents)
    text = "\n\n".join(f"===== {d.path.name} =====\n{d.text}" for d in docs if d.text.strip())
    if not text:
        req = WorkRequirements(notes=["Методичка не найдена или не содержит извлекаемого текста."], confidence="low")
        save_requirements(project, req)
        return req

    local = _local_extract(text, settings)
    if use_ai and settings.any_ai_ready:
        provider = HybridRouter(settings).reviewer()
        prompt = f"""
Извлеки требования к учебной работе ТОЛЬКО из текста методических материалов ниже.
Не додумывай отсутствующие требования. Если значение не найдено — null/пустой список.
Верни только JSON по схеме:
{{
  "work_type": null,
  "pages_min": null,
  "pages_max": null,
  "chapters": null,
  "sections_per_chapter_min": null,
  "sources_min": null,
  "source_recency_years": null,
  "introduction_requirements": [],
  "conclusion_requirements": [],
  "tables_required": null,
  "figures_required": null,
  "calculations_required": null,
  "applications_required": null,
  "formatting": {{
    "font_name": null,
    "font_size_pt": null,
    "heading1_size_pt": null,
    "heading2_size_pt": null,
    "heading3_size_pt": null,
    "line_spacing": null,
    "first_line_indent_cm": null,
    "margin_left_cm": null,
    "margin_right_cm": null,
    "margin_top_cm": null,
    "margin_bottom_cm": null,
    "alignment": null
  }},
  "notes": []
}}

МАТЕРИАЛЫ:
{text[:90_000]}
""".strip()
        try:
            raw = provider.complete([{"role": "user", "content": prompt}], max_tokens=3500, temperature=0.0)
            data = _extract_json(raw)
            req = _merge_requirements_raw(local, data)
            req.confidence = "high"
        except Exception as exc:
            local.notes.append(f"AI-анализ требований не выполнен: {exc}")
            req = local
    else:
        req = local
    save_requirements(project, req)
    return req


def requirements_summary(req: WorkRequirements) -> str:
    f = req.formatting
    return "\n".join([
        "📘 Требования проекта",
        f"Объём: {req.pages_min or '?'}–{req.pages_max or '?'} стр.",
        f"Глав: {req.chapters or '?'}",
        f"Минимум источников: {req.sources_min or '?'}",
        f"Шрифт: {f.font_name}, {f.font_size_pt:g} pt",
        f"Интервал: {f.line_spacing:g}",
        f"Абзац: {f.first_line_indent_cm:g} см",
        f"Поля (Л/П/В/Н): {f.margin_left_cm:g}/{f.margin_right_cm:g}/{f.margin_top_cm:g}/{f.margin_bottom_cm:g} см",
        f"Уверенность: {req.confidence}",
        *(f"• {x}" for x in req.notes[:8]),
    ])


def _local_extract(text: str, settings: Settings) -> WorkRequirements:
    lower = text.casefold()
    profile = FormattingProfile(
        font_name=settings.default_font_name,
        font_size_pt=settings.default_font_size_pt,
        line_spacing=settings.default_line_spacing,
        first_line_indent_cm=settings.default_first_line_indent_cm,
        margin_left_cm=settings.default_margin_left_cm,
        margin_right_cm=settings.default_margin_right_cm,
        margin_top_cm=settings.default_margin_top_cm,
        margin_bottom_cm=settings.default_margin_bottom_cm,
    )
    if "times new roman" in lower or "таймс нью роман" in lower:
        profile.font_name = "Times New Roman"
    m = re.search(r"(?:кегл|размер\s+шрифта|шрифт[^\n]{0,60}?)(1[0-8])\s*(?:pt|пт|кегл)?", lower)
    if m:
        profile.font_size_pt = float(m.group(1))
    m = re.search(r"(?:межстрочн\w*\s+интервал|интервал)[^\n]{0,40}?(1(?:[.,]\d+)?)", lower)
    if m:
        profile.line_spacing = float(m.group(1).replace(",", "."))
    m = re.search(r"(?:абзац\w*\s+отступ|первая\s+строка)[^\n]{0,40}?(\d+[.,]?\d*)\s*см", lower)
    if m:
        profile.first_line_indent_cm = float(m.group(1).replace(",", "."))

    # Поля: извлекаем только явные пары "левое 30 мм" и т.п.
    for key, words in [
        ("margin_left_cm", r"лев\w*"),
        ("margin_right_cm", r"прав\w*"),
        ("margin_top_cm", r"верх\w*"),
        ("margin_bottom_cm", r"ниж\w*"),
    ]:
        m = re.search(words + r"\s*(?:поле)?[^\d\n]{0,20}(\d+(?:[.,]\d+)?)\s*(мм|см)", lower)
        if m:
            value = float(m.group(1).replace(",", "."))
            if m.group(2) == "мм":
                value /= 10
            setattr(profile, key, value)
    profile._normalize()

    pages_min = pages_max = None
    m = re.search(r"(?:объ[её]м|объем)[^\n]{0,80}?(\d{2,3})\s*[-–—]\s*(\d{2,3})\s*(?:стр|страниц)", lower)
    if m:
        pages_min, pages_max = int(m.group(1)), int(m.group(2))
    sources_min = None
    m = re.search(r"(?:не\s+менее|минимум)[^\n]{0,40}?(\d{1,3})[^\n]{0,30}(?:источник|наименован|литератур)", lower)
    if m:
        sources_min = int(m.group(1))
    chapters = None
    m = re.search(r"(?:состоять|состоит)[^\n]{0,50}?из\s+(\d)\s+глав", lower)
    if m:
        chapters = int(m.group(1))

    return WorkRequirements(
        pages_min=pages_min,
        pages_max=pages_max,
        chapters=chapters,
        sources_min=sources_min,
        formatting=profile,
        confidence="partial",
        notes=["Локально извлечены только явно распознаваемые требования; после подключения AI анализ будет глубже."],
    )


def _merge_requirements_raw(local: WorkRequirements, raw: dict[str, Any]) -> WorkRequirements:
    data = local.to_dict()
    scalar_keys = [
        "work_type", "pages_min", "pages_max", "chapters", "sections_per_chapter_min",
        "sources_min", "source_recency_years", "tables_required", "figures_required",
        "calculations_required", "applications_required",
    ]
    for key in scalar_keys:
        value = raw.get(key)
        if value not in (None, ""):
            data[key] = value
    for key in ("introduction_requirements", "conclusion_requirements", "notes"):
        value = raw.get(key)
        if isinstance(value, list) and value:
            data[key] = value
    fmt = dict(data.get("formatting") or {})
    raw_fmt = raw.get("formatting") if isinstance(raw.get("formatting"), dict) else {}
    for key, value in raw_fmt.items():
        if value not in (None, ""):
            fmt[key] = value
    data["formatting"] = fmt
    return WorkRequirements.from_dict(data)


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON не найден")
    data = json.loads(cleaned[start:end+1])
    if not isinstance(data, dict):
        raise ValueError("ожидался JSON object")
    return data


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except Exception:
        return None


def _bool_or_none(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if v in (None, ""):
        return None
    if str(v).casefold() in {"true", "1", "yes", "да"}:
        return True
    if str(v).casefold() in {"false", "0", "no", "нет"}:
        return False
    return None


def _str_list(v: Any) -> list[str]:
    return [str(x).strip() for x in v] if isinstance(v, list) else []
