from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from projects import get_project, list_materials
from providers.router import HybridRouter
from readers import SourceDocument
from settings import Settings


@dataclass(slots=True)
class StyleProfile:
    tone: str = "academic_neutral"
    sentence_length: str = "mixed"
    paragraph_size: str = "medium"
    terminology: str = "professional"
    first_person: bool = False
    notes: list[str] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


STYLE_FILE = "style_profile.json"


def style_path(project: Path) -> Path:
    return project / STYLE_FILE


def load_style(project: Path) -> StyleProfile:
    path = style_path(project)
    if not path.exists():
        return StyleProfile(notes=[])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return StyleProfile(**{k: data.get(k) for k in StyleProfile.__dataclass_fields__ if k in data})
    except Exception:
        return StyleProfile(notes=[])


def extract_style_profile(settings: Settings, project_name: str, documents: Sequence[SourceDocument]) -> StyleProfile:
    project = get_project(settings, project_name)
    materials = list_materials(settings, project_name)
    role_by_path = {str(x.get("path")): str(x.get("role")) for x in materials}
    samples: list[str] = []
    for doc in documents:
        try:
            rel = str(doc.path.relative_to(project)).replace("\\", "/")
        except ValueError:
            rel = doc.path.name
        if role_by_path.get(rel) == "author_style_sample" and doc.text.strip():
            samples.append(doc.text[:25_000])
    if not samples:
        profile = StyleProfile(notes=[])
        _save(project, profile)
        return profile

    sample = "\n\n".join(samples)[:60_000]
    if settings.any_ai_ready:
        provider = HybridRouter(settings).reviewer()
        prompt = f"""
Определи ТОЛЬКО стилистический профиль автора по примеру. Не копируй предложения и не извлекай факты.
Верни JSON:
{{
  "tone": "academic_neutral",
  "sentence_length": "mixed",
  "paragraph_size": "medium",
  "terminology": "professional",
  "first_person": false,
  "notes": ["..."]
}}

Оцени длину предложений, формальность, терминологию, длину абзаца и структуру аргументации.
ТЕКСТ-ПРИМЕР:
{sample}
""".strip()
        try:
            raw = provider.complete([{"role": "user", "content": prompt}], max_tokens=1000, temperature=0.0)
            data = _json(raw)
            profile = StyleProfile(
                tone=str(data.get("tone") or "academic_neutral"),
                sentence_length=str(data.get("sentence_length") or "mixed"),
                paragraph_size=str(data.get("paragraph_size") or "medium"),
                terminology=str(data.get("terminology") or "professional"),
                first_person=bool(data.get("first_person", False)),
                notes=[str(x) for x in data.get("notes", []) if str(x).strip()],
            )
        except Exception:
            profile = _local_style(sample)
    else:
        profile = _local_style(sample)
    _save(project, profile)
    return profile


def style_prompt(profile: StyleProfile) -> str:
    return (
        f"Стиль: tone={profile.tone}; sentence_length={profile.sentence_length}; "
        f"paragraph_size={profile.paragraph_size}; terminology={profile.terminology}; "
        f"first_person={profile.first_person}. Не копируй фразы из примера стиля."
    )


def _local_style(text: str) -> StyleProfile:
    sentences = [x.strip() for x in re.split(r"[.!?]+", text) if x.strip()]
    avg = sum(len(x.split()) for x in sentences) / max(len(sentences), 1)
    if avg < 11:
        length = "short_to_medium"
    elif avg > 22:
        length = "medium_to_long"
    else:
        length = "mixed"
    paragraphs = [x for x in text.splitlines() if x.strip()]
    avg_p = sum(len(x.split()) for x in paragraphs) / max(len(paragraphs), 1)
    psize = "short" if avg_p < 55 else "long" if avg_p > 150 else "medium"
    first_person = bool(re.search(r"\b(?:я|мы)\s+(?:счита|полага|рассматрива|анализиру)", text.casefold()))
    return StyleProfile(sentence_length=length, paragraph_size=psize, first_person=first_person, notes=["Профиль получен локальной статистикой без AI."])


def _save(project: Path, profile: StyleProfile) -> None:
    style_path(project).write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    a, b = cleaned.find("{"), cleaned.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("JSON not found")
    return json.loads(cleaned[a:b+1])
