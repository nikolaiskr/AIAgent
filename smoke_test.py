from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from academic_renderer import AcademicDocxRenderer
from assembler import assemble_work
from pipeline import create_work_plan
from projects import create_project, prepare_upload_target, register_uploaded_file, set_material_role, update_project_field
from requirements_engine import WorkRequirements, save_requirements
from settings import load_settings
from workspace import load_work_plan, save_section_summary, save_section_version


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aiagent_v3_test_") as tmp:
        settings = replace(load_settings(), projects_root=Path(tmp) / "Курсовые")
        project = create_project(settings, "Тест", "coursework", owner_user_id=123)
        update_project_field(settings, project.name, "topic", "Тестовая тема")

        target = prepare_upload_target(settings, project.name, "Методичка.txt")
        target.write_text("Объем 30–40 страниц. Times New Roman 14. Межстрочный интервал 1.5.", encoding="utf-8")
        register_uploaded_file(settings, project.name, target)
        rel = str(target.relative_to(project)).replace("\\", "/")
        set_material_role(settings, project.name, rel, "methodology")

        req = WorkRequirements(pages_min=30, pages_max=40)
        save_requirements(project, req)
        plan = create_work_plan(settings, project.name, force=True)
        assert plan.sections

        renderer = AcademicDocxRenderer(req.formatting)
        # Для smoke test создаём локальные mock-версии вместо вызова AI.
        for node in plan.leaves():
            if node.id.casefold() in {"references", "literature", "списоклитературы"}:
                continue
            text = f"Тестовый текст раздела {node.id} {node.title}. " * 25
            save_section_version(
                project,
                node,
                text,
                reason="smoke_test",
                status="approved",
                docx_builder=lambda path, body, n=node: renderer.render(path, body, title=f"{n.id} {n.title}"),
                review={"score": 100, "issues": [], "required_changes": []},
            )
            save_section_summary(project, node, f"- Завершён тестовый раздел {node.id}.")

        result = assemble_work(settings, project.name, allow_unapproved=True)
        assert result.docx_path.exists()
        assert result.report_path.exists()
        assert (project / "work_plan.json").exists()
        assert (project / "sections").exists()
        print("SMOKE TEST: OK")
        print(f"Temp project: {project}")
        print(f"Final DOCX: {result.docx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
