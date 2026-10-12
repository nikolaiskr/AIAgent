from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Sequence

from workspace import SectionNode, WorkPlan, latest_section_version


@dataclass(slots=True)
class SimilarityIssue:
    kind: str
    message: str
    section_id: str | None = None
    score: float | None = None


@dataclass(slots=True)
class ConsistencyReport:
    issues: list[SimilarityIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def text(self) -> str:
        if not self.issues:
            return "✅ Локальная проверка согласованности: явных проблем не найдено."
        lines = ["🔍 Локальная проверка согласованности:"]
        for issue in self.issues:
            score = f" ({issue.score:.0%})" if issue.score is not None else ""
            lines.append(f"• {issue.message}{score}")
        return "\n".join(lines)


def internal_similarity_check(
    project: Path,
    plan: WorkPlan,
    current: SectionNode,
    text: str,
    *,
    threshold: float = 0.72,
) -> ConsistencyReport:
    issues: list[SimilarityIssue] = []
    current_blocks = _paragraphs(text)
    for node in plan.leaves():
        if node.id == current.id:
            continue
        _, other_text = latest_section_version(project, node)
        if not other_text:
            continue
        other_blocks = _paragraphs(other_text)
        max_score = 0.0
        for a in current_blocks[:80]:
            if len(a) < 120:
                continue
            for b in other_blocks[:80]:
                if len(b) < 120:
                    continue
                score = SequenceMatcher(None, _norm(a), _norm(b)).ratio()
                max_score = max(max_score, score)
                if score >= threshold:
                    issues.append(
                        SimilarityIssue(
                            kind="section_duplicate",
                            section_id=node.id,
                            score=score,
                            message=f"В новом разделе есть крупный фрагмент, похожий на раздел {node.id} «{node.title}»",
                        )
                    )
                    break
            if max_score >= threshold:
                break

    repeated = _repeated_openers(text)
    for phrase, count in repeated:
        issues.append(
            SimilarityIssue(
                kind="template_repetition",
                message=f"Вводная конструкция «{phrase}» повторяется {count} раз",
            )
        )
    return ConsistencyReport(issues=issues)


def final_local_consistency(project: Path, plan: WorkPlan) -> ConsistencyReport:
    issues: list[SimilarityIssue] = []
    seen_titles: dict[str, str] = {}
    for node in plan.walk():
        key = _norm(node.title)
        if key in seen_titles and len(key) > 8:
            issues.append(SimilarityIssue(kind="duplicate_title", message=f"Похожие названия разделов: {seen_titles[key]} и {node.id}"))
        seen_titles[key] = node.id

    for node in plan.leaves():
        version, text = latest_section_version(project, node)
        if version is None:
            issues.append(SimilarityIssue(kind="missing_section", section_id=node.id, message=f"Раздел {node.id} «{node.title}» ещё не создан"))
        elif version.status not in {"approved", "completed"}:
            issues.append(SimilarityIssue(kind="not_approved", section_id=node.id, message=f"Раздел {node.id} имеет статус {version.status}"))
        elif not text or len(text.strip()) < 100:
            issues.append(SimilarityIssue(kind="too_short", section_id=node.id, message=f"Раздел {node.id} подозрительно короткий"))
    return ConsistencyReport(issues=issues)


def introduction_conclusion_task_check(project: Path, plan: WorkPlan) -> list[str]:
    intro = next((n for n in plan.leaves() if n.id.casefold() in {"intro", "introduction", "введение"}), None)
    conclusion = next((n for n in plan.leaves() if n.id.casefold() in {"conclusion", "заключение"}), None)
    if not intro or not conclusion:
        return []
    _, intro_text = latest_section_version(project, intro)
    _, concl_text = latest_section_version(project, conclusion)
    if not intro_text or not concl_text:
        return []
    tasks = re.findall(r"(?:задач[аи]\s+исследования[^:]*:)?\s*(?:\d+[.)]|[-•])\s*([^\n]{20,220})", intro_text, flags=re.I)
    if not tasks:
        return ["⚠️ Не удалось автоматически выделить задачи из введения."]
    lines = []
    lower = _norm(concl_text)
    for i, task in enumerate(tasks[:12], start=1):
        keywords = [w for w in re.findall(r"[а-яёa-z]{5,}", _norm(task)) if w not in {"исследование", "провести", "определить", "рассмотреть"}]
        hits = sum(1 for w in set(keywords[:10]) if w in lower)
        lines.append(("✅" if hits >= 2 else "⚠️") + f" Задача {i}: {'есть связанный вывод' if hits >= 2 else 'явный связанный вывод не найден'}")
    return lines


def _paragraphs(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"\n\s*\n|\n", text) if len(x.strip()) >= 40]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-zа-яё0-9 ]", " ", text.casefold())).strip()


def _repeated_openers(text: str) -> list[tuple[str, int]]:
    patterns = [
        "таким образом",
        "следует отметить",
        "важно подчеркнуть",
        "необходимо отметить",
        "в современном мире",
        "на сегодняшний день",
        "в рамках данного исследования",
        "стоит отметить",
    ]
    low = text.casefold()
    counts = [(p, low.count(p)) for p in patterns]
    return [(p, c) for p, c in counts if c >= 3]
