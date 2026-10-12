from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from academic_renderer import AcademicDocxRenderer
from consistency import final_local_consistency, introduction_conclusion_task_check
from ingest import ingest_project
from projects import get_project, load_project_meta
from providers.router import HybridRouter
from requirements_engine import WorkRequirements, load_requirements
from settings import Settings
from source_registry import bibliography_text, build_registry, citation_number_map
from workspace import WorkPlan, latest_section_version, load_work_plan


class AssemblyError(RuntimeError):
    pass


@dataclass(slots=True)
class AssemblyResult:
    docx_path: Path
    report_path: Path
    included: list[str]
    warnings: list[str] = field(default_factory=list)


def assemble_work(
    settings: Settings,
    project_name: str,
    *,
    allow_unapproved: bool = False,
    progress: Callable[[str], None] | None = None,
) -> AssemblyResult:
    project = get_project(settings, project_name)
    plan = load_work_plan(project)
    if plan is None:
        raise AssemblyError("Сначала создай work_plan.json.")

    local = final_local_consistency(project, plan)
    blocking = [x for x in local.issues if x.kind in {"missing_section", "not_approved"}]
    if blocking and not allow_unapproved:
        message = "\n".join(f"• {x.message}" for x in blocking[:20])
        raise AssemblyError("Не все разделы готовы/approved:\n" + message)

    _progress(progress, "Собираю последние версии разделов…")
    parts: list[tuple[str, str]] = []
    included: list[str] = []
    warnings: list[str] = []

    # Не добавляем отдельный title для parent-глав, если есть leaf-подразделы: рендерер соберёт их последовательно.
    for node in plan.leaves():
        version, text = latest_section_version(project, node)
        if not version or not text:
            warnings.append(f"Пропущен {node.id}: нет версии")
            continue
        if version.status not in {"approved", "completed"}:
            warnings.append(f"{node.id}: включена версия со статусом {version.status}")
        if node.id.casefold() in {"references", "literature", "списоклитературы"}:
            # bibliography формируем из registry ниже, а не из LLM-списка.
            continue
        parts.append((f"{node.id} {node.title}", text))
        included.append(f"{node.id} v{version.version}")

    if not parts:
        raise AssemblyError("Нет разделов для сборки.")

    ingest = ingest_project(settings, project_name, allow_vision=False)
    registry = build_registry(settings, project_name, ingest.documents)
    if registry:
        parts.append(("Список литературы", bibliography_text(registry)))

    req = load_requirements(project) or WorkRequirements()
    renderer = AcademicDocxRenderer(req.formatting)
    output = project / "output"
    output.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    docx_path = output / f"Курсовая_финальная_{stamp}.docx"
    meta = load_project_meta(settings, project_name)
    renderer.render_combined(
        docx_path,
        parts,
        title=str(meta.get("topic") or meta.get("name") or project_name),
        source_number_map=citation_number_map(registry),
    )

    _progress(progress, "Проверяю финальную согласованность…")
    report_lines = [local.text(), ""]
    report_lines.extend(introduction_conclusion_task_check(project, plan))

    if settings.openai_ready:
        try:
            provider = HybridRouter(settings).reviewer()
            joined = "\n\n".join(f"# {h}\n{t}" for h, t in parts)
            prompt = f"""
Проведи финальную consistency-проверку уже написанной учебной работы. НЕ переписывай работу.
Проверь: структуру, нумерацию, повторы, терминологию, цели и задачи, соответствие выводов задачам,
единообразие названий, согласованность чисел, SOURCE_ID/ссылки и список литературы.
Отдельно проверь введение ↔ заключение. Не придумывай требований.
Верни краткий отчёт с ✅ / ⚠️ / ❌ и конкретными местами.

ПЛАН:
{json.dumps(plan.to_dict(), ensure_ascii=False)}

ТЕКСТ:
{joined[:110_000]}
""".strip()
            report_lines += ["", "AI Final Consistency Review:", provider.complete([{"role": "user", "content": prompt}], max_tokens=5000, temperature=0.0)]
        except Exception as exc:
            warnings.append(f"AI Final Consistency Review не выполнен: {exc}")

    report_path = output / f"Финальная_проверка_{stamp}.txt"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    return AssemblyResult(docx_path=docx_path, report_path=report_path, included=included, warnings=warnings)


def _progress(cb, message: str) -> None:
    if cb:
        try:
            cb(message)
        except Exception:
            pass
