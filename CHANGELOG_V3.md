# AIAgent V3 — реализация правок

## Реализовано в коде

- WorkPlan + `work_plan.json`.
- Статусы sections.
- `sections/<id>/v1/v2/...` + metadata + review + summary.
- Генерация отдельного раздела.
- Продолжение существующего раздела без перезаписи.
- Доработка конкретного раздела по замечанию.
- Сборка главы без повторной генерации.
- ProjectMemory.
- Summaries предыдущих разделов.
- RequirementAnalyzer + `requirements.json`.
- Проверка/repair минимальной структуры плана.
- SourceRegistry + SOURCE_ID.
- CitationValidator.
- Role-aware BM25 retrieval.
- Writer → Reviewer → Editor loop.
- MAX_REVIEW_LOOPS.
- NaturalAcademicEditor.
- StyleProfile из author_style_sample.
- Internal similarity checker между разделами.
- AcademicDocxRenderer с AcademicPreset и MethodologyProfile.
- FinalAssembler по существующим последним версиям.
- Final local consistency check + optional OpenAI review.
- Introduction ↔ conclusion local task heuristic.
- Vision images + scan-PDF fallback через OpenAI, с cache.
- Ownership в project.json.
- Background JobManager, progress, cancel, SQLite status.
- Новое Telegram-меню проекта и разделов.
- Загрузка файлов из Telegram.
- Скачивание текущих и предыдущих версий sections.
- CLI для plan/section/chapter/assemble.

## Оставлено как production upgrade, а не обязательная локальная зависимость

- Redis/RQ/Celery: V3 использует совместимый по смыслу локальный JobManager, чтобы Windows-тест не требовал Redis.
- Embeddings API: retrieval уже BM25 + keyword + role weights; semantic embeddings можно добавить следующим слоем без изменения структуры sections.
- Полноценный библиографический парсер всех ГОСТ-вариантов: V3 принципиально не выдумывает недостающие metadata и использует filename fallback.
- Массовое горизонтальное исполнение нескольких серверных worker-нод.
