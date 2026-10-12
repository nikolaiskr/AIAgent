from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Sequence

from projects import get_project, list_materials
from readers import SourceDocument
from settings import Settings


ROLE_WEIGHTS = {
    "methodology": 2.0,
    "assignment": 1.8,
    "feedback": 2.2,
    "source_data": 1.7,
    "literature": 1.3,
    "example": 0.7,
    "author_style_sample": 0.6,
    "additional": 1.0,
    "current_version": 2.0,
    "image": 1.0,
    "other": 0.8,
}

TASK_ROLE_PRIORITY = {
    "generate": {"methodology", "assignment", "literature", "source_data", "additional"},
    "revise": {"current_version", "feedback", "methodology", "assignment"},
    "calculate": {"assignment", "source_data", "methodology", "example"},
    "final_check": {"methodology", "assignment", "current_version", "feedback"},
    "requirements": {"methodology"},
}

STOP = {
    "это", "как", "для", "что", "или", "при", "над", "под", "все", "всех", "где",
    "нужно", "работа", "раздел", "глава", "the", "and", "with", "from", "будет", "быть",
}


@dataclass(slots=True)
class RetrievalChunk:
    source_name: str
    source_path: str
    role: str
    text: str
    index: int
    bm25_score: float = 0.0
    keyword_score: float = 0.0
    role_score: float = 0.0
    numeric_score: float = 0.0
    total_score: float = 0.0


def tokenize(text: str) -> list[str]:
    return [x for x in re.findall(r"[a-zа-яё0-9]{2,}", text.casefold()) if x not in STOP]


def chunk_documents(
    settings: Settings,
    project_name: str,
    documents: Sequence[SourceDocument],
) -> list[RetrievalChunk]:
    project = get_project(settings, project_name)
    material_roles = {str(x.get("path")): str(x.get("role") or "other") for x in list_materials(settings, project_name)}
    result: list[RetrievalChunk] = []
    step = max(1, settings.chunk_chars - settings.chunk_overlap)
    for doc in documents:
        try:
            rel = str(doc.path.relative_to(project)).replace("\\", "/")
        except ValueError:
            rel = doc.path.name
        role = material_roles.get(rel, "other")
        text = doc.text.strip()
        if not text:
            continue
        pieces = [text] if len(text) <= settings.chunk_chars else [
            text[start:start + settings.chunk_chars].strip()
            for start in range(0, len(text), step)
            if text[start:start + settings.chunk_chars].strip()
        ]
        for i, part in enumerate(pieces):
            result.append(RetrievalChunk(doc.path.name, rel, role, part, i))
    return result


def retrieve(
    settings: Settings,
    project_name: str,
    documents: Sequence[SourceDocument],
    query: str,
    *,
    task_type: str = "generate",
    top_k: int | None = None,
    max_chars: int | None = None,
) -> tuple[str, list[RetrievalChunk]]:
    chunks = chunk_documents(settings, project_name, documents)
    if not chunks:
        return "[Подходящих текстовых материалов нет.]", []

    q_tokens = tokenize(query)
    q_set = set(q_tokens)
    numbers = set(re.findall(r"\d+(?:[.,]\d+)?", query))
    corpus_tokens = [tokenize(c.text) for c in chunks]
    n = len(chunks)
    avgdl = sum(len(x) for x in corpus_tokens) / max(n, 1)
    df: dict[str, int] = {}
    for tokens in corpus_tokens:
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1

    preferred = TASK_ROLE_PRIORITY.get(task_type, set())
    k1, b = 1.5, 0.75
    for chunk, tokens in zip(chunks, corpus_tokens):
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        dl = max(len(tokens), 1)
        bm25 = 0.0
        for term in q_tokens:
            if term not in tf:
                continue
            dfi = df.get(term, 0)
            idf = math.log(1 + (n - dfi + 0.5) / (dfi + 0.5))
            freq = tf[term]
            bm25 += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / max(avgdl, 1)))
        chunk.bm25_score = bm25
        overlap = len(q_set & set(tokens))
        chunk.keyword_score = overlap * 0.8
        base_role = ROLE_WEIGHTS.get(chunk.role, 1.0)
        chunk.role_score = base_role + (1.2 if chunk.role in preferred else 0.0)
        lower = chunk.text.casefold()
        chunk.numeric_score = sum(1.5 for num in numbers if num.casefold() in lower)
        chunk.total_score = chunk.bm25_score + chunk.keyword_score + chunk.role_score + chunk.numeric_score

    ranked = sorted(chunks, key=lambda x: x.total_score, reverse=True)
    chosen = ranked[: (top_k or settings.top_chunks)]
    limit = max_chars or settings.max_context_chars
    blocks: list[str] = []
    total = 0
    for chunk in chosen:
        block = (
            f"\n===== {chunk.source_name} | role={chunk.role} | score={chunk.total_score:.2f} =====\n"
            f"{chunk.text}\n"
        )
        if total + len(block) > limit:
            remaining = limit - total
            if remaining > 1000:
                blocks.append(block[:remaining])
            break
        blocks.append(block)
        total += len(block)
    return "".join(blocks).strip(), chosen
