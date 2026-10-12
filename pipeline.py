from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from academic_renderer import AcademicDocxRenderer, FormattingProfile
from consistency import internal_similarity_check
from ingest import IngestResult, ingest_project
from jobs import JobCancelled
from projects import get_project, load_project_meta, save_project_meta
from providers.base import OutputTruncatedError
from providers.router import HybridRouter
from requirements_engine import WorkRequirements, analyze_requirements, load_requirements, save_requirements
from retrieval import RetrievalChunk, retrieve
from settings import Settings
from source_registry import (
    SourceRecord,
    build_registry,
    citation_number_map,
    source_context,
    validate_citations,
)
from style_profile import StyleProfile, extract_style_profile, load_style, style_prompt
from workspace import (
    ProjectMemory,
    SectionNode,
    WorkPlan,
    default_work_plan,
    latest_section_version,
    load_memory,
    load_section_summary,
    load_work_plan,
    plan_text,
    previous_summaries,
    save_memory,
    save_section_summary,
    save_section_version,
    save_work_plan,
    set_section_status,
)


class PipelineError(RuntimeError):
    pass


ProgressCallback = Callable[[str], None]


@dataclass(slots=True)
class ReviewResult:
    score: int = 0
    issues: list[str] = field(default_factory=list)
    required_changes: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    citation_issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "issues": self.issues,
            "required_changes": self.required_changes,
            "strengths": self.strengths,
            "citation_issues": self.citation_issues,
        }


@dataclass(slots=True)
class SectionGenerationResult:
    node: SectionNode
    version: int
    status: str
    score: int
    text: str
    docx_path: Path
    md_path: Path
    summary_path: Path
    review_path: Path | None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ChapterGenerationResult:
    parent: SectionNode
    docx_path: Path
    included_sections: list[str]
    warnings: list[str] = field(default_factory=list)


def ensure_requirements(
    settings: Settings,
    project_name: str,
    *,
    progress: ProgressCallback | None = None,
    force: bool = False,
) -> WorkRequirements:
    project = get_project(settings, project_name)
    existing = load_requirements(project)
    if existing is not None and not force:
        return existing
    _progress(progress, "Анализирую методические требования…")
    ingest = ingest_project(settings, project_name, allow_vision=True)
    req = analyze_requirements(settings, project_name, ingest.documents, use_ai=True)
    meta = load_project_meta(settings, project_name)
    meta["requirements_status"] = "analyzed"
    save_project_meta(project, meta)
    return req


def create_work_plan(
    settings: Settings,
    project_name: str,
    *,
    progress: ProgressCallback | None = None,
    force: bool = False,
) -> WorkPlan:
    project = get_project(settings, project_name)
    existing = load_work_plan(project)
    if existing is not None and not force:
        return existing

    meta = load_project_meta(settings, project_name)
    topic = str(meta.get("topic") or meta.get("name") or project_name)
    work_type = str(meta.get("type") or "other")
    req = ensure_requirements(settings, project_name, progress=progress)
    target_pages = req.pages_max or req.pages_min

    if settings.any_ai_ready:
        _progress(progress, "Строю структуру работы…")
        ingest = ingest_project(settings, project_name, allow_vision=True)
        assignment = _role_text(settings, project_name, ingest, {"assignment"}, limit=35_000)
        methodology = _role_text(settings, project_name, ingest, {"methodology"}, limit=35_000)
        provider = HybridRouter(settings).reviewer()
        prompt = f"""
Составь план учебной работы. Строго учитывай тему и методические требования.
План должен быть реалистичным по объёму. Для курсовой/ВКР включай введение, главы с подразделами,
заключение и список литературы. Не придумывай фактические данные о компании.
Верни ТОЛЬКО JSON:
{{
  "title": "...",
  "work_type": "{work_type}",
  "target_pages": {target_pages if target_pages else 'null'},
  "sections": [
    {{"id":"intro","title":"Введение","target_pages":2,"status":"planned","children":[]}},
    {{"id":"1","title":"...","target_pages":10,"status":"planned","children":[
      {{"id":"1.1","title":"...","target_pages":3,"status":"planned","children":[]}}
    ]}}
  ]
}}

ТЕМА:
{topic}

ТРЕБОВАНИЯ:
{json.dumps(req.to_dict(), ensure_ascii=False)}

ЗАДАНИЕ:
{assignment}

МЕТОДИЧКА:
{methodology}
""".strip()
        try:
            raw = provider.complete([{"role": "user", "content": prompt}], max_tokens=5000, temperature=0.1)
            data = _extract_json(raw)
            plan = WorkPlan.from_dict(data)
            if not plan.sections:
                raise ValueError("модель вернула пустой план")
            _validate_and_repair_plan(plan, req)
        except Exception as exc:
            plan = default_work_plan(topic, work_type, target_pages)
            _validate_and_repair_plan(plan, req)
    else:
        plan = default_work_plan(topic, work_type, target_pages)
        _validate_and_repair_plan(plan, req)

    save_work_plan(project, plan)
    meta = load_project_meta(settings, project_name)
    meta["plan_status"] = "created"
    save_project_meta(project, meta)
    return plan


def initialize_project_memory(
    settings: Settings,
    project_name: str,
    *,
    force: bool = False,
) -> ProjectMemory:
    project = get_project(settings, project_name)
    current = load_memory(project)
    if not force and any([
        current.goal,
        current.research_object,
        current.research_subject,
        current.tasks,
        current.organization,
    ]):
        return current

    meta = load_project_meta(settings, project_name)
    plan = load_work_plan(project) or create_work_plan(settings, project_name)
    if not settings.any_ai_ready:
        current.goal = current.goal or (f"Раскрыть тему «{meta.get('topic')}»" if meta.get("topic") else None)
        save_memory(project, current)
        return current

    ingest = ingest_project(settings, project_name, allow_vision=True)
    context = _role_text(settings, project_name, ingest, {"assignment", "methodology", "source_data"}, limit=50_000)
    provider = HybridRouter(settings).reviewer()
    prompt = f"""
Сформируй память проекта учебной работы. Извлекай только то, что подтверждается темой/заданием/материалами.
Не выдумывай организацию, период, числа или определения.
Верни JSON:
{{
 "research_object": null,
 "research_subject": null,
 "goal": null,
 "tasks": [],
 "terminology": {{}},
 "organization": null,
 "time_period": null,
 "accepted_definitions": {{}},
 "important_numbers": {{}},
 "claims": {{}}
}}

ТЕМА: {meta.get('topic') or meta.get('name')}
ПЛАН: {plan_text(plan)}
МАТЕРИАЛЫ:
{context}
""".strip()
    try:
        data = _extract_json(provider.complete([{"role": "user", "content": prompt}], max_tokens=2500, temperature=0.0))
        current = ProjectMemory.from_dict(data)
    except Exception:
        current.goal = current.goal or (f"Раскрыть тему «{meta.get('topic')}»" if meta.get("topic") else None)
    save_memory(project, current)
    return current


def generate_section(
    settings: Settings,
    project_name: str,
    section_id: str,
    *,
    user_instruction: str = "",
    reason: str = "generation",
    feedback_file: str | None = None,
    progress: ProgressCallback | None = None,
) -> SectionGenerationResult:
    """Публичная обёртка: гарантирует, что статус раздела не застрянет в «writing».

    При ошибке раздел уходит в needs_revision, при отмене пользователем —
    возвращается в предыдущий статус, чтобы не портить статистику готовности.
    """
    project = get_project(settings, project_name)
    plan = load_work_plan(project) or create_work_plan(settings, project_name, progress=progress)
    node = plan.find(section_id)
    if node is None:
        raise PipelineError(f"Раздел «{section_id}» не найден в work_plan.json.")
    if node.children:
        raise PipelineError("Выбранная сущность — глава с подразделами. Используй генерацию подразделов или сборку главы.")
    latest, _ = latest_section_version(project, node)
    prev_status = latest.status if latest else "planned"
    set_section_status(project, node, "writing")
    try:
        return _generate_section_impl(
            settings,
            project_name,
            section_id,
            user_instruction=user_instruction,
            reason=reason,
            feedback_file=feedback_file,
            progress=progress,
        )
    except JobCancelled:
        set_section_status(project, node, prev_status)
        raise
    except OutputTruncatedError as exc:
        set_section_status(project, node, "needs_revision")
        raise PipelineError(
            f"Раздел {node.id}: модель обрезала ответ по лимиту токенов. "
            "Увеличь MAX_OUTPUT_TOKENS в config.env или разбей раздел на два."
        ) from exc
    except Exception:
        set_section_status(project, node, "needs_revision")
        raise


def _generate_section_impl(
    settings: Settings,
    project_name: str,
    section_id: str,
    *,
    user_instruction: str = "",
    reason: str = "generation",
    feedback_file: str | None = None,
    progress: ProgressCallback | None = None,
) -> SectionGenerationResult:
    if not settings.any_ai_ready:
        raise PipelineError("Для генерации раздела подключи DeepSeek или OpenAI API.")

    project = get_project(settings, project_name)
    plan = load_work_plan(project) or create_work_plan(settings, project_name, progress=progress)
    node = plan.find(section_id)
    if node is None:
        raise PipelineError(f"Раздел «{section_id}» не найден в work_plan.json.")
    if node.children:
        raise PipelineError("Выбранная сущность — глава с подразделами. Используй генерацию подразделов или сборку главы.")

    _progress(progress, f"Подготавливаю контекст раздела {node.id}…")
    set_section_status(project, node, "writing")
    ingest = ingest_project(settings, project_name, allow_vision=True)
    req = load_requirements(project) or analyze_requirements(settings, project_name, ingest.documents, use_ai=True)
    memory = initialize_project_memory(settings, project_name)
    style = load_style(project)
    if style.notes is None or (style.notes == [] and _has_role(settings, project_name, "author_style_sample")):
        style = extract_style_profile(settings, project_name, ingest.documents)
    registry = build_registry(settings, project_name, ingest.documents)
    previous = previous_summaries(project, plan, node.id)

    task_type = "revise" if reason != "generation" else "generate"
    query = _section_query(project_name, plan, node, user_instruction, memory)
    _, chosen = retrieve(
        settings,
        project_name,
        ingest.documents,
        query,
        task_type=task_type,
        top_k=settings.top_chunks,
        max_chars=settings.max_context_chars,
    )
    context = _context_with_source_ids(chosen, registry, settings.max_context_chars)

    latest, latest_text = latest_section_version(project, node)
    source_version = latest.version if latest else None
    current_text = latest_text or ""
    feedback_text = _feedback_text(settings, project_name, ingest, feedback_file) if reason != "generation" else ""

    router = HybridRouter(settings)
    writer = router.writer()
    soul = _read_soul(settings)
    _progress(progress, f"Пишу раздел {node.id}…")
    draft_prompt = _writer_prompt(
        soul=soul,
        plan=plan,
        node=node,
        req=req,
        memory=memory,
        style=style,
        registry=registry,
        previous=previous,
        context=context,
        instruction=user_instruction,
        current_text=current_text,
        feedback_text=feedback_text,
        reason=reason,
    )
    try:
        draft = writer.complete(
            [{"role": "system", "content": soul}, {"role": "user", "content": draft_prompt}],
            max_tokens=settings.max_output_tokens,
            temperature=0.18,
        )
    except OutputTruncatedError:
        set_section_status(project, node, "needs_revision")
        raise
    except Exception as exc:
        set_section_status(project, node, "needs_revision")
        raise PipelineError(f"Writer не смог создать раздел: {exc}") from exc

    warnings = list(ingest.warnings)
    citation_ok, citation_issues = validate_citations(draft, registry)
    similarity = internal_similarity_check(project, plan, node, draft)
    if similarity.issues:
        warnings.extend(x.message for x in similarity.issues)

    _progress(progress, "Проверяю содержание и аргументацию…")
    review = _review_section(settings, node, plan, req, memory, registry, draft, citation_issues, similarity.text())
    edited = draft
    loops = 0
    while loops < settings.max_review_loops and (review.score < settings.review_score_threshold or review.required_changes or review.citation_issues):
        loops += 1
        _progress(progress, f"Редактирую по замечаниям, цикл {loops}/{settings.max_review_loops}…")
        edited = _edit_section(settings, node, req, registry, edited, review)
        citation_ok, citation_issues = validate_citations(edited, registry)
        review = _review_section(settings, node, plan, req, memory, registry, edited, citation_issues, similarity.text())

    if settings.use_natural_editor:
        _progress(progress, "Привожу текст к естественному академическому стилю…")
        try:
            edited = _natural_academic_edit(settings, node, req, style, registry, edited)
        except Exception as exc:
            warnings.append(f"NaturalAcademicEditor не выполнен: {exc}")

    # Финальная проверка после стилистического прохода.
    citation_ok, citation_issues = validate_citations(edited, registry)
    final_review = _review_section(settings, node, plan, req, memory, registry, edited, citation_issues, "")
    final_score = final_review.score
    approved = final_score >= settings.review_score_threshold and not citation_issues
    status = "approved" if approved else "needs_revision"

    _progress(progress, "Сохраняю версию раздела и DOCX…")
    renderer = AcademicDocxRenderer(req.formatting)
    number_map = citation_number_map(registry)

    def build_docx(path: Path, text: str) -> None:
        renderer.render(path, text, title=f"{node.id} {node.title}", source_number_map=number_map)

    version = save_section_version(
        project,
        node,
        edited,
        reason=reason,
        source_version=source_version,
        feedback_file=feedback_file,
        status=status,
        docx_builder=build_docx,
        review=final_review.to_dict(),
    )
    summary = _create_summary(settings, node, edited)
    summary_path = save_section_summary(project, node, summary)
    _update_memory_after_section(settings, project, node, summary, edited)

    # Путь к разделу формируется централизованно, чтобы версии всегда попадали в одну и ту же папку.
    from workspace import section_dir as _section_dir
    folder = _section_dir(project, node)
    md_path = folder / (version.md_file or f"v{version.version}.md")
    docx_path = folder / (version.docx_file or f"v{version.version}.docx")
    review_path = folder / version.review_file if version.review_file else None

    return SectionGenerationResult(
        node=node,
        version=version.version,
        status=status,
        score=final_score,
        text=edited,
        docx_path=docx_path,
        md_path=md_path,
        summary_path=summary_path,
        review_path=review_path,
        warnings=warnings + final_review.issues + final_review.citation_issues,
    )


def continue_section(
    settings: Settings,
    project_name: str,
    section_id: str,
    instruction: str,
    *,
    progress: ProgressCallback | None = None,
) -> SectionGenerationResult:
    return generate_section(
        settings,
        project_name,
        section_id,
        user_instruction=instruction,
        reason="continue_section",
        progress=progress,
    )


def revise_section(
    settings: Settings,
    project_name: str,
    section_id: str,
    instruction: str,
    *,
    feedback_file: str | None = None,
    progress: ProgressCallback | None = None,
) -> SectionGenerationResult:
    return generate_section(
        settings,
        project_name,
        section_id,
        user_instruction=instruction,
        reason="revision_after_feedback" if feedback_file else "manual_revision",
        feedback_file=feedback_file,
        progress=progress,
    )


def generate_chapter(
    settings: Settings,
    project_name: str,
    chapter_id: str,
    *,
    progress: ProgressCallback | None = None,
) -> ChapterGenerationResult:
    project = get_project(settings, project_name)
    plan = load_work_plan(project)
    if plan is None:
        raise PipelineError("Сначала создай план работы.")
    parent = plan.find(chapter_id)
    if parent is None:
        raise PipelineError(f"Глава {chapter_id} не найдена.")
    nodes = parent.children if parent.children else [parent]
    pieces: list[tuple[str, str]] = []
    warnings: list[str] = []
    for node in nodes:
        version, text = latest_section_version(project, node)
        if not version or not text:
            warnings.append(f"Раздел {node.id} ещё не создан")
            continue
        pieces.append((f"{node.id} {node.title}", text))
    if not pieces:
        raise PipelineError("В главе нет ни одного созданного раздела.")
    req = load_requirements(project) or WorkRequirements()
    registry = build_registry(settings, project_name, ingest_project(settings, project_name).documents)
    renderer = AcademicDocxRenderer(req.formatting)
    output = project / "output"
    output.mkdir(exist_ok=True)
    name = _safe_filename(f"Глава_{parent.id}_{parent.title}.docx")
    path = output / name
    renderer.render_combined(path, pieces, title=f"{parent.id} {parent.title}", source_number_map=citation_number_map(registry))
    _progress(progress, f"Глава {parent.id} собрана без повторной генерации текста.")
    return ChapterGenerationResult(parent=parent, docx_path=path, included_sections=[x[0] for x in pieces], warnings=warnings)


def section_candidates(settings: Settings, project_name: str, *, leaves_only: bool = True) -> list[SectionNode]:
    project = get_project(settings, project_name)
    plan = load_work_plan(project)
    if plan is None:
        return []
    return plan.leaves() if leaves_only else list(plan.walk())


def _writer_prompt(
    *,
    soul: str,
    plan: WorkPlan,
    node: SectionNode,
    req: WorkRequirements,
    memory: ProjectMemory,
    style: StyleProfile,
    registry: Sequence[SourceRecord],
    previous: str,
    context: str,
    instruction: str,
    current_text: str,
    feedback_text: str,
    reason: str,
) -> str:
    revision_block = ""
    if current_text:
        revision_block = f"""
ТЕКУЩАЯ ВЕРСИЯ РАЗДЕЛА:
{current_text[:45_000]}

ЗАМЕЧАНИЯ / ДОПОЛНИТЕЛЬНАЯ ИНСТРУКЦИЯ:
{feedback_text[:20_000]}
{instruction}

Это редактирование существующего раздела. Не пиши новый несвязанный текст с нуля: сохрани полезное содержание и внеси требуемые изменения.
"""
    return f"""
Ты Writer одного раздела учебной работы. Пиши ТОЛЬКО раздел {node.id} «{node.title}».
Не пиши всю курсовую целиком и не повторяй содержание уже завершённых разделов.

ЦЕЛЕВОЙ ОБЪЁМ: {node.target_pages or 'по содержанию и методичке'} страниц.
ПЛАН ВСЕЙ РАБОТЫ:
{plan_text(plan)}

ТРЕБОВАНИЯ:
{json.dumps(req.to_dict(), ensure_ascii=False)}

PROJECT MEMORY:
{json.dumps(memory.to_dict(), ensure_ascii=False)}

ПРОФИЛЬ СТИЛЯ:
{style_prompt(style)}

КРАТКИЕ ИТОГИ ПРЕДЫДУЩИХ РАЗДЕЛОВ:
{previous or '[нет]'}

SOURCE REGISTRY:
{source_context(registry)}

РЕЛЕВАНТНЫЕ ФРАГМЕНТЫ:
{context}

{revision_block}

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:
1. Не выдумывай источники, авторов, годы, факты, числа и данные.
2. Ссылаться разрешено только на зарегистрированные источники в формате [SOURCE_001].
3. Если утверждение опирается на источник — ставь SOURCE_ID рядом с утверждением.
4. Не копируй длинные куски источников; пересказывай и анализируй, сохраняя ссылку.
5. Не делай пустых абзацев об «актуальности» без объяснения причин.
6. Каждый существенный абзац должен определять, объяснять, сопоставлять, анализировать или делать вывод.
7. Не злоупотребляй вводными «Таким образом», «Следует отметить», «Важно подчеркнуть».
8. Не искажай профессиональные термины ради синонимов.
9. Не добавляй разделы, которых нет в задании. Верни только текст выбранного раздела.
10. Формат — обычный академический текст с подзаголовками Markdown при необходимости, без HTML.
""".strip()


def _review_section(
    settings: Settings,
    node: SectionNode,
    plan: WorkPlan,
    req: WorkRequirements,
    memory: ProjectMemory,
    registry: Sequence[SourceRecord],
    text: str,
    citation_issues: Sequence[str],
    local_similarity: str,
) -> ReviewResult:
    provider = HybridRouter(settings).reviewer()
    prompt = f"""
Ты Reviewer. Не переписывай текст. Оцени раздел {node.id} «{node.title}» по шкале 0–100.
Проверь: соответствие теме, полноту, логику, аргументацию, повторы, источники, неподтверждённые утверждения,
структуру, соответствие методичке и согласованность с ProjectMemory.
Верни ТОЛЬКО JSON:
{{"score":85,"issues":[],"required_changes":[],"strengths":[]}}

ТРЕБОВАНИЯ: {json.dumps(req.to_dict(), ensure_ascii=False)}
PROJECT MEMORY: {json.dumps(memory.to_dict(), ensure_ascii=False)}
SOURCE REGISTRY: {source_context(registry)}
ЛОКАЛЬНЫЕ ПРОБЛЕМЫ ССЫЛОК: {list(citation_issues)}
ЛОКАЛЬНАЯ ПРОВЕРКА ПОВТОРОВ: {local_similarity}

ТЕКСТ:
{text[:90_000]}
""".strip()
    try:
        data = _extract_json(provider.complete([{"role": "user", "content": prompt}], max_tokens=2500, temperature=0.0))
        score = max(0, min(int(data.get("score") or 0), 100))
        return ReviewResult(
            score=score,
            issues=_list_str(data.get("issues")),
            required_changes=_list_str(data.get("required_changes")),
            strengths=_list_str(data.get("strengths")),
            citation_issues=list(citation_issues),
        )
    except Exception as exc:
        score = 70 if citation_issues else 80
        return ReviewResult(score=score, issues=[f"Reviewer JSON не получен: {exc}"], citation_issues=list(citation_issues))


def _edit_section(
    settings: Settings,
    node: SectionNode,
    req: WorkRequirements,
    registry: Sequence[SourceRecord],
    text: str,
    review: ReviewResult,
) -> str:
    provider = HybridRouter(settings).editor()
    prompt = f"""
Ты Editor раздела {node.id} «{node.title}». Исправь ТОЛЬКО перечисленные проблемы Reviewer.
Сохрани проверенные факты, числа, названия и SOURCE_ID. Не добавляй новых фактов без контекста и не создавай новых источников.
Верни полностью исправленный текст раздела без комментариев.

ТРЕБОВАНИЯ: {json.dumps(req.to_dict(), ensure_ascii=False)}
SOURCE REGISTRY: {source_context(registry)}
REVIEW: {json.dumps(review.to_dict(), ensure_ascii=False)}

ТЕКСТ:
{text}
""".strip()
    return provider.complete([{"role": "user", "content": prompt}], max_tokens=settings.max_output_tokens, temperature=0.08)


def _natural_academic_edit(
    settings: Settings,
    node: SectionNode,
    req: WorkRequirements,
    style: StyleProfile,
    registry: Sequence[SourceRecord],
    text: str,
) -> str:
    provider = HybridRouter(settings).editor()
    prompt = f"""
Ты NaturalAcademicEditor. Улучши естественность академического русского текста раздела {node.id}, НЕ меняя фактическое содержание.

Нельзя:
- менять числа, названия, выводы и SOURCE_ID;
- добавлять факты или источники;
- придумывать ошибки или намеренно портить грамматику.

Нужно:
- уменьшить шаблонные повторяющиеся вводные конструкции;
- разнообразить длину и синтаксис предложений;
- убрать чрезмерный канцелярит и бессодержательные абзацы;
- убрать лишние повторы слов, не заменяя точные термины бессмысленными синонимами;
- усилить связность аргументации и промежуточные выводы, только если они логически следуют из уже написанного.

ПРОФИЛЬ: {style_prompt(style)}
SOURCE REGISTRY: {source_context(registry)}

Верни только итоговый текст.

{text}
""".strip()
    return provider.complete([{"role": "user", "content": prompt}], max_tokens=settings.max_output_tokens, temperature=0.12)


def _create_summary(settings: Settings, node: SectionNode, text: str) -> str:
    if settings.any_ai_ready:
        provider = HybridRouter(settings).reviewer()
        prompt = f"""
Сделай краткую память раздела {node.id} «{node.title}» для следующих разделов курсовой.
5–10 пунктов: основные положения, принятые определения, важные данные/числа, промежуточные выводы и то,
что должно быть использовано дальше. Не добавляй ничего, чего нет в тексте.

{text[:60_000]}
""".strip()
        try:
            return provider.complete([{"role": "user", "content": prompt}], max_tokens=1600, temperature=0.0)
        except Exception:
            pass
    paragraphs = [x.strip() for x in text.splitlines() if len(x.strip()) > 60]
    return "\n".join(f"- {x[:400]}" for x in paragraphs[:8])


def _update_memory_after_section(settings: Settings, project: Path, node: SectionNode, summary: str, text: str) -> None:
    memory = load_memory(project)
    if settings.any_ai_ready:
        provider = HybridRouter(settings).reviewer()
        prompt = f"""
Обнови ProjectMemory на основании ТОЛЬКО завершённого раздела. Не удаляй существующие корректные данные.
Не выдумывай факты. Верни JSON той же структуры:
{json.dumps(memory.to_dict(), ensure_ascii=False)}

РАЗДЕЛ {node.id}:
{summary}
""".strip()
        try:
            data = _extract_json(provider.complete([{"role": "user", "content": prompt}], max_tokens=1800, temperature=0.0))
            memory = ProjectMemory.from_dict(data)
        except Exception:
            memory.claims[node.id] = summary[:3000]
    else:
        memory.claims[node.id] = summary[:3000]
    save_memory(project, memory)


def _validate_and_repair_plan(plan: WorkPlan, req: WorkRequirements) -> None:
    # Детерминированные минимальные проверки перед генерацией.
    ids = {x.id for x in plan.walk()}
    if plan.work_type in {"coursework", "thesis"}:
        if not any(x.casefold() in {"intro", "introduction", "введение"} for x in ids):
            plan.sections.insert(0, SectionNode("intro", "Введение", 2, "planned"))
        if not any(x.casefold() in {"conclusion", "заключение"} for x in ids):
            plan.sections.append(SectionNode("conclusion", "Заключение", 2, "planned"))
        if not any(x.casefold() in {"references", "literature", "списоклитературы"} for x in ids):
            plan.sections.append(SectionNode("references", "Список литературы", 2, "planned"))
    for node in plan.walk():
        if node.status == "pending":
            node.status = "planned"

    if req.chapters:
        chapters = [x for x in plan.sections if re.fullmatch(r"\d+", x.id.strip())]
        if len(chapters) < req.chapters:
            for i in range(len(chapters) + 1, req.chapters + 1):
                plan.sections.insert(-2 if len(plan.sections) >= 2 else len(plan.sections), SectionNode(str(i), f"Глава {i}", None, "planned", []))
    chapters = [x for x in plan.sections if re.fullmatch(r"\d+", x.id.strip())]
    if req.sections_per_chapter_min:
        for chapter in chapters:
            next_num = len(chapter.children) + 1
            while len(chapter.children) < req.sections_per_chapter_min:
                chapter.children.append(SectionNode(f"{chapter.id}.{next_num}", f"Подраздел {chapter.id}.{next_num}", None, "planned", []))
                next_num += 1


def _context_with_source_ids(chosen: Sequence[RetrievalChunk], registry: Sequence[SourceRecord], max_chars: int) -> str:
    id_by_path = {x.relative_path: x.id for x in registry}
    blocks: list[str] = []
    total = 0
    for c in chosen:
        sid = id_by_path.get(c.source_path)
        header = f"===== {c.source_name} | role={c.role}"
        if sid:
            header += f" | source_id={sid}"
        header += " =====\n"
        block = header + c.text + "\n"
        if total + len(block) > max_chars:
            break
        blocks.append(block)
        total += len(block)
    return "\n".join(blocks)


def _section_query(project_name: str, plan: WorkPlan, node: SectionNode, instruction: str, memory: ProjectMemory) -> str:
    return " ".join([
        plan.title,
        node.id,
        node.title,
        instruction,
        memory.organization or "",
        memory.time_period or "",
        " ".join(memory.tasks),
    ]).strip()


def _role_text(settings: Settings, project_name: str, ingest: IngestResult, roles: set[str], *, limit: int) -> str:
    project = get_project(settings, project_name)
    from projects import list_materials
    role_by_path = {str(x.get("path")): str(x.get("role")) for x in list_materials(settings, project_name)}
    blocks = []
    total = 0
    for doc in ingest.documents:
        try:
            rel = str(doc.path.relative_to(project)).replace("\\", "/")
        except ValueError:
            rel = doc.path.name
        if role_by_path.get(rel) not in roles:
            continue
        block = f"\n===== {doc.path.name} =====\n{doc.text}\n"
        if total + len(block) > limit:
            break
        blocks.append(block)
        total += len(block)
    return "".join(blocks).strip() or "[нет]"


def _has_role(settings: Settings, project_name: str, role: str) -> bool:
    from projects import list_materials
    return any(str(x.get("role")) == role for x in list_materials(settings, project_name))


def _feedback_text(settings: Settings, project_name: str, ingest: IngestResult, feedback_file: str | None) -> str:
    if feedback_file:
        for doc in ingest.documents:
            if doc.path.name == Path(feedback_file).name:
                return doc.text[:30_000]
    return _role_text(settings, project_name, ingest, {"feedback"}, limit=30_000)


def _read_soul(settings: Settings) -> str:
    candidates = [settings.projects_root / "soul.md", Path(__file__).resolve().parent / "soul.md"]
    for path in candidates:
        if path.exists():
            for enc in ("utf-8-sig", "utf-8", "cp1251"):
                try:
                    return path.read_text(encoding=enc).strip()
                except UnicodeDecodeError:
                    continue
                except OSError:
                    break
    return "Ты — аккуратный учебный ассистент. Не выдумывай данные и источники. Соблюдай методические требования."


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    a, b = cleaned.find("{"), cleaned.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("JSON не найден")
    data = json.loads(cleaned[a:b+1])
    if not isinstance(data, dict):
        raise ValueError("ожидался JSON object")
    return data


def _list_str(value: Any) -> list[str]:
    return [str(x).strip() for x in value if str(x).strip()] if isinstance(value, list) else []


def _progress(cb: ProgressCallback | None, message: str) -> None:
    if cb:
        try:
            cb(message)
        except JobCancelled:
            # Отмена пользователем должна прерывать pipeline, а не глотаться.
            raise
        except Exception:
            pass


def _safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:140] or "document.docx"


def generate_all_sections(
    settings: Settings,
    project_name: str,
    *,
    progress: ProgressCallback | None = None,
    skip_existing_approved: bool = True,
) -> list[SectionGenerationResult]:
    """Генерирует работу по разделам, а не одним огромным LLM-запросом."""
    project = get_project(settings, project_name)
    plan = load_work_plan(project) or create_work_plan(settings, project_name, progress=progress)
    results: list[SectionGenerationResult] = []
    leaves = [n for n in plan.leaves() if n.id.casefold() not in {"references", "literature", "списоклитературы"}]
    for idx, node in enumerate(leaves, start=1):
        latest, _ = latest_section_version(project, node)
        if skip_existing_approved and latest and latest.status in {"approved", "completed"}:
            _progress(progress, f"[{idx}/{len(leaves)}] {node.id} уже approved — пропускаю")
            continue
        _progress(progress, f"[{idx}/{len(leaves)}] Генерирую {node.id} {node.title}")
        results.append(generate_section(settings, project_name, node.id, progress=progress))
    return results


def validate_plan_against_requirements(plan: WorkPlan, req: WorkRequirements) -> list[str]:
    lines: list[str] = []
    ids = {x.id.casefold() for x in plan.walk()}
    if plan.work_type in {"coursework", "thesis"}:
        lines.append("✅ введение" if ids & {"intro", "introduction", "введение"} else "❌ нет введения")
        lines.append("✅ заключение" if ids & {"conclusion", "заключение"} else "❌ нет заключения")
    chapters = [x for x in plan.sections if re.fullmatch(r"\d+", x.id.strip())]
    if req.chapters is not None:
        lines.append(f"{'✅' if len(chapters) >= req.chapters else '❌'} глав: {len(chapters)} / требуется {req.chapters}")
    if req.sections_per_chapter_min:
        for chapter in chapters:
            lines.append(
                f"{'✅' if len(chapter.children) >= req.sections_per_chapter_min else '⚠️'} "
                f"{chapter.id}: подразделов {len(chapter.children)} / минимум {req.sections_per_chapter_min}"
            )
    if req.pages_min or req.pages_max:
        target = plan.target_pages
        ok = target is not None and (req.pages_min is None or target >= req.pages_min) and (req.pages_max is None or target <= req.pages_max)
        lines.append(f"{'✅' if ok else '⚠️'} целевой объём плана: {target or '?'} стр.; методичка {req.pages_min or '?'}–{req.pages_max or '?'}")
    return lines
