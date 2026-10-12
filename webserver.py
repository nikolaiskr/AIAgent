from __future__ import annotations

"""HTTP-сервер Telegram Mini App.

Сервер статики (webapp/) и JSON API в одном процессе с ботом.
Авторизация — валидация Telegram WebApp initData (HMAC-SHA256 с ключом бота).
Все тяжёлые pipeline-операции уходят в тот же JobManager, что и бот;
прогресс фронтенд поллит через /api/jobs/<id>.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import billing
import notify
from jobs import JobCancelled, JobManager, ProjectBusyError, get_job, recent_jobs
from settings import Settings

logger = logging.getLogger("aiagent.web")

try:
    from aiohttp import web
    AIOHTTP_OK = True
except ImportError:  # pragma: no cover
    web = None  # type: ignore[assignment]
    AIOHTTP_OK = False

WEBAPP_DIR = Path(__file__).resolve().parent

KIND_LABELS = {
    "generate_section": "Генерация раздела",
    "continue_section": "Продолжение раздела",
    "feedback_revision": "Правка по замечанию",
    "create_plan": "Создание плана",
    "generate_all": "Работа по разделам",
    "generate_chapter": "Сборка главы",
    "assemble_work": "Сборка работы",
    "assemble_force": "Сборка (force)",
    "standalone_task": "Отдельное задание",
}


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, code: str = "ERROR") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------- auth

def check_init_data(init_data: str, bot_token: str) -> dict[str, Any] | None:
    """Стандартная проверка подписи Telegram WebApp initData."""
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received = parsed.pop("hash", "")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received):
            return None
        user = json.loads(parsed.get("user", "{}"))
        return user if isinstance(user, dict) and user.get("id") else None
    except Exception:
        return None


if AIOHTTP_OK:

    def _apply_cors(response: web.StreamResponse, origin: str) -> None:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"

    @web.middleware
    async def auth_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
        settings: Settings = request.app["settings"]
        origin = request.headers.get("Origin", "")
        allowed = tuple(settings.allowed_origins or ())
        cors_origin = origin if origin in allowed else None

        # CORS-preflight: отвечаем до авторизации.
        if request.method == "OPTIONS" and cors_origin:
            response = web.Response(status=204)
            _apply_cors(response, cors_origin)
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data, X-Dev-User"
            response.headers["Access-Control-Max-Age"] = "86400"
            return response

        def with_cors(resp: web.StreamResponse) -> web.StreamResponse:
            if cors_origin:
                _apply_cors(resp, cors_origin)
            return resp

        if request.path == "/" or request.path.startswith("/static/"):
            return with_cors(await handler(request))
        user: dict[str, Any] | None = None
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        if init_data and settings.telegram_bot_token:
            user = await asyncio.to_thread(check_init_data, init_data, settings.telegram_bot_token)
        if user is None and settings.dev_mode:
            dev = request.headers.get("X-Dev-User", "")
            if dev.isdigit():
                user = {"id": int(dev), "username": "dev", "first_name": "Dev"}
        if user is None:
            return with_cors(web.json_response(
                {"ok": False, "error": "Не удалось проверить вход Telegram. Открой приложение через бота.", "code": "AUTH"},
                status=401,
            ))
        request["tg_user"] = user
        try:
            billing.touch_user(int(user["id"]), user.get("username"), user.get("first_name"))
        except Exception:
            logger.debug("touch_user failed", exc_info=True)
        try:
            return with_cors(await handler(request))
        except ApiError as exc:
            return with_cors(web.json_response({"ok": False, "error": str(exc), "code": exc.code}, status=exc.status))
        except json.JSONDecodeError:
            return with_cors(web.json_response({"ok": False, "error": "Некорректный JSON в запросе.", "code": "BAD_JSON"}, status=400))
        except Exception as exc:
            logger.exception("API error: %s %s", request.method, request.path)
            notify.owner_message(
                settings,
                f"🐛 <b>Ошибка Mini App API</b>\n<code>{request.method} {request.path}</code>\n"
                f"Код: <code>{notify.err_code(exc)}</code>\n<code>{str(exc)[:600]}</code>",
                kind="api_error",
                user_id=int(user["id"]),
            )
            return with_cors(web.json_response(
                {"ok": False, "error": f"Внутренняя ошибка сервиса. Код: {notify.err_code(exc)}", "code": "INTERNAL"},
                status=500,
            ))


def _user_id(request) -> int:
    return int(request["tg_user"]["id"])


def _settings(request) -> Settings:
    return request.app["settings"]


def _jobs(request) -> JobManager:
    return request.app["jobs"]


def _submit(request, project: str, kind: str, func) -> str:
    try:
        return _jobs(request).submit(user_id=_user_id(request), project=project, kind=kind, func=func)
    except ProjectBusyError as exc:
        raise ApiError(409, str(exc), "PROJECT_BUSY")


def _owned(settings: Settings, user_id: int, name: str) -> None:
    from projects import ensure_project_owner, get_project
    get_project(settings, name)
    ensure_project_owner(settings, name, user_id)


def _result_payload(info) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": info.id,
        "kind": info.kind,
        "kind_label": KIND_LABELS.get(info.kind, info.kind),
        "project": info.project,
        "status": info.status,
        "progress": info.progress,
        "created_at": info.created_at,
        "updated_at": info.updated_at,
        "error": info.error,
        "result": None,
    }
    if info.result_json:
        try:
            payload["result"] = json.loads(info.result_json)
        except json.JSONDecodeError:
            payload["result"] = {"raw": info.result_json[:2000]}
    return payload


# ---------------------------------------------------------------- handlers (sync parts)

def _do_project_card(settings: Settings, user_id: int, name: str) -> dict[str, Any]:
    from projects import load_project_meta
    from workspace import WorkPlan, load_work_plan, plan_text
    meta = load_project_meta(settings, name)
    plan = load_work_plan(settings.projects_root / name)
    done = total = pct = 0
    if plan is not None:
        done, total, pct = plan.progress()
    files = meta.get("files", [])
    return {
        "name": meta.get("name", name),
        "type": meta.get("type") or "other",
        "topic": meta.get("topic"),
        "status": meta.get("status"),
        "updated_at": meta.get("updated_at"),
        "materials": len(files),
        "sections_done": done,
        "sections_total": total,
        "pct": pct,
        "owner": meta.get("owner_user_id") in (None, "") or int(meta.get("owner_user_id")) == int(user_id),
    }


def _do_project_detail(settings: Settings, user_id: int, name: str) -> dict[str, Any]:
    from projects import list_materials, load_project_meta
    from workspace import load_work_plan, plan_text
    card = _do_project_card(settings, user_id, name)
    meta = load_project_meta(settings, name)
    plan = load_work_plan(settings.projects_root / name)
    sections: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    if plan is not None:
        from workspace import latest_section_version
        project_path = settings.projects_root / name
        for node in plan.walk():
            version, text = latest_section_version(project_path, node)
            entry = {
                "id": node.id,
                "title": node.title,
                "status": node.status,
                "target_pages": node.target_pages,
                "children": len(node.children),
                "version": version.version if version else None,
                "versions_count": 0,
                "words": version.word_count if version else 0,
                "has_text": bool(text),
            }
            if version is not None:
                from workspace import list_section_versions
                entry["versions_count"] = len(list_section_versions(project_path, node))
            (chapters if node.children else sections).append(entry)
    materials = []
    for item in list_materials(settings, name):
        materials.append({
            "path": item.get("path"),
            "name": item.get("name"),
            "extension": item.get("extension"),
            "size": item.get("size") or 0,
            "role": item.get("role") or "other",
            "role_manual": bool(item.get("role_manual")),
        })
    card.update({
        "variant": meta.get("variant"),
        "plan": plan.to_dict() if plan else None,
        "plan_text": plan_text(plan) if plan else None,
        "sections": sections,
        "chapters": chapters,
        "materials": materials,
    })
    return card


# ---------------------------------------------------------------- aiohttp handlers

if AIOHTTP_OK:

    async def index(request: web.Request) -> web.StreamResponse:
        index_path = WEBAPP_DIR / "index.html"
        if not index_path.exists():
            return web.Response(text="Mini App фронтенд не найден (webapp/index.html).", content_type="text/plain")
        return web.FileResponse(index_path)

    async def static_file(request: web.Request) -> web.StreamResponse:
        filename = request.match_info["filename"]
        if "/" in filename or ".." in filename:
            raise ApiError(404, "Not found", "NOT_FOUND")
        path = WEBAPP_DIR / "static" / filename
        if not path.exists() or not path.is_file():
            raise ApiError(404, "Not found", "NOT_FOUND")
        return web.FileResponse(path)

    async def api_me(request: web.Request) -> web.Response:
        settings = _settings(request)
        user = request["tg_user"]
        uid = int(user["id"])
        return web.json_response({
            "ok": True,
            "user": {"id": uid, "username": user.get("username"), "first_name": user.get("first_name")},
            "admin": notify.is_admin(settings, uid),
            "quota": billing.quota_status(settings, uid),
            "owner_link": notify.owner_link(settings),
            "miniapp_url": settings.miniapp_url,
            "kind_labels": KIND_LABELS,
        })

    async def api_projects(request: web.Request) -> web.Response:
        settings = _settings(request)
        from projects import list_projects_for_user
        names = [p.name for p in await asyncio.to_thread(list_projects_for_user, settings, _user_id(request))]
        cards = await asyncio.gather(*[
            asyncio.to_thread(_do_project_card, settings, _user_id(request), name) for name in names
        ])
        return web.json_response({"ok": True, "projects": cards})

    async def api_project_create(request: web.Request) -> web.Response:
        settings = _settings(request)
        body = await request.json()
        name = str(body.get("name") or "").strip()
        work_type = str(body.get("type") or "other")
        if not name:
            raise ApiError(400, "Введи название работы.", "VALIDATION")
        uid = _user_id(request)
        try:
            await asyncio.to_thread(billing.check_project_quota, settings, uid)
        except billing.PaymentRequired as exc:
            raise ApiError(402, str(exc), "PAYWALL")
        from projects import create_project
        project = await asyncio.to_thread(create_project, settings, name, work_type, uid)
        billing.log_event("project_created", f"{work_type}: {project.name}", user_id=uid, project=project.name)
        return web.json_response({"ok": True, "name": project.name})

    async def api_project_detail(request: web.Request) -> web.Response:
        settings = _settings(request)
        name = request.match_info["name"]
        await asyncio.to_thread(_owned, settings, _user_id(request), name)
        detail = await asyncio.to_thread(_do_project_detail, settings, _user_id(request), name)
        return web.json_response({"ok": True, "project": detail})

    async def api_upload(request: web.Request) -> web.Response:
        settings = _settings(request)
        name = request.match_info["name"]
        await asyncio.to_thread(_owned, settings, _user_id(request), name)
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.filename is None:
            raise ApiError(400, "Нет файла в запросе.", "VALIDATION")
        from projects import prepare_upload_target, register_uploaded_file
        original = Path(field.filename).name
        target = await asyncio.to_thread(prepare_upload_target, settings, name, original)
        size = 0
        with open(target, "wb") as fh:
            while True:
                chunk = await field.read_chunk(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                size += len(chunk)
        if size > settings.max_upload_mb * 1024 * 1024:
            target.unlink(missing_ok=True)
            raise ApiError(413, f"Файл больше лимита {settings.max_upload_mb} МБ.", "TOO_LARGE")
        item = await asyncio.to_thread(register_uploaded_file, settings, name, target)
        return web.json_response({"ok": True, "file": {"name": item.get("name"), "path": item.get("path"), "role": item.get("role")}})

    async def api_material_role(request: web.Request) -> web.Response:
        settings = _settings(request)
        name = request.match_info["name"]
        await asyncio.to_thread(_owned, settings, _user_id(request), name)
        body = await request.json()
        rel = str(body.get("path") or "")
        role = str(body.get("role") or "")
        from projects import set_material_role
        await asyncio.to_thread(set_material_role, settings, name, rel, role)
        return web.json_response({"ok": True})

    async def api_material_delete(request: web.Request) -> web.Response:
        settings = _settings(request)
        name = request.match_info["name"]
        await asyncio.to_thread(_owned, settings, _user_id(request), name)
        rel = request.query.get("path", "")
        from projects import delete_material
        await asyncio.to_thread(delete_material, settings, name, rel)
        return web.json_response({"ok": True})

    async def api_plan_create(request: web.Request) -> web.Response:
        settings = _settings(request)
        name = request.match_info["name"]
        await asyncio.to_thread(_owned, settings, _user_id(request), name)
        body = await request.json() if request.can_read_body else {}
        force = bool(body.get("force"))
        import pipeline
        job_id = _submit(request, name, "create_plan",
                         lambda progress: pipeline.create_work_plan(settings, name, progress=progress, force=force))
        return web.json_response({"ok": True, "job_id": job_id})

    async def api_plan_put(request: web.Request) -> web.Response:
        settings = _settings(request)
        name = request.match_info["name"]
        await asyncio.to_thread(_owned, settings, _user_id(request), name)
        body = await request.json()
        from workspace import WorkPlan, load_work_plan, save_work_plan
        plan = WorkPlan.from_dict(body)
        if not plan.sections:
            raise ApiError(400, "План должен содержать хотя бы один раздел.", "VALIDATION")
        await asyncio.to_thread(save_work_plan, settings.projects_root / name, plan)
        return web.json_response({"ok": True, "plan": load_work_plan(settings.projects_root / name).to_dict()})

    async def api_section_action(request: web.Request) -> web.Response:
        settings = _settings(request)
        name = request.match_info["name"]
        sid = request.match_info["sid"]
        action = request.match_info["action"]
        await asyncio.to_thread(_owned, settings, _user_id(request), name)
        body = await request.json() if request.can_read_body else {}
        instruction = str(body.get("instruction") or "")
        import pipeline
        if action == "generate":
            kind, reason = "generate_section", "generation"
        elif action == "continue":
            kind, reason = "continue_section", "continue_section"
        elif action == "revise":
            kind, reason = "feedback_revision", "manual_revision"
        else:
            raise ApiError(404, "Unknown action", "NOT_FOUND")
        job_id = _submit(
            request, name, kind,
            lambda progress: pipeline.generate_section(
                settings, name, sid,
                user_instruction=instruction, reason=reason, progress=progress,
            ),
        )
        return web.json_response({"ok": True, "job_id": job_id})

    async def api_chapter(request: web.Request) -> web.Response:
        settings = _settings(request)
        name = request.match_info["name"]
        await asyncio.to_thread(_owned, settings, _user_id(request), name)
        body = await request.json()
        chapter_id = str(body.get("chapter_id") or "")
        import pipeline
        job_id = _submit(request, name, "generate_chapter",
                         lambda progress: pipeline.generate_chapter(settings, name, chapter_id, progress=progress))
        return web.json_response({"ok": True, "job_id": job_id})

    async def api_assemble(request: web.Request) -> web.Response:
        settings = _settings(request)
        name = request.match_info["name"]
        await asyncio.to_thread(_owned, settings, _user_id(request), name)
        body = await request.json() if request.can_read_body else {}
        force = bool(body.get("force"))
        from assembler import assemble_work
        job_id = _submit(request, name, "assemble_force" if force else "assemble_work",
                         lambda progress: assemble_work(settings, name, allow_unapproved=force, progress=progress))
        return web.json_response({"ok": True, "job_id": job_id})

    async def api_jobs(request: web.Request) -> web.Response:
        infos = await asyncio.to_thread(recent_jobs, _user_id(request), 15)
        return web.json_response({"ok": True, "jobs": [_result_payload(i) for i in infos]})

    async def api_job_info(request: web.Request) -> web.Response:
        info = get_job(request.match_info["job_id"])
        if info is None or int(info.user_id) != _user_id(request):
            raise ApiError(404, "Задача не найдена.", "NOT_FOUND")
        return web.json_response({"ok": True, "job": _result_payload(info)})

    async def api_job_cancel(request: web.Request) -> web.Response:
        info = get_job(request.match_info["job_id"])
        if info is None or int(info.user_id) != _user_id(request):
            raise ApiError(404, "Задача не найдена.", "NOT_FOUND")
        _jobs(request).cancel(info.id)
        return web.json_response({"ok": True})

    async def api_download(request: web.Request) -> web.StreamResponse:
        settings = _settings(request)
        name = request.query.get("project", "")
        rel = request.query.get("path", "")
        await asyncio.to_thread(_owned, settings, _user_id(request), name)
        from projects import get_project
        project = get_project(settings, name)
        target = (project / rel).resolve()
        try:
            target.relative_to(project.resolve())
        except ValueError:
            raise ApiError(403, "Недопустимый путь.", "FORBIDDEN")
        if not target.exists() or not target.is_file():
            raise ApiError(404, "Файл не найден.", "NOT_FOUND")
        response = web.FileResponse(target)
        filename = urllib.parse.quote(target.name)
        response.headers["Content-Disposition"] = f'attachment; filename*=UTF-8\'\'{filename}'
        return response

    async def api_payment_request(request: web.Request) -> web.Response:
        settings = _settings(request)
        user = request["tg_user"]
        uid = int(user["id"])
        quota = billing.quota_status(settings, uid)
        if quota["paid"]:
            return web.json_response({"ok": True, "already_paid": True})
        billing.log_event("payment_request", f"quota {quota['used']}/{quota['limit']}", user_id=uid)
        notify.payment_request(settings, uid, user.get("username"), user.get("first_name"), quota["used"], quota["limit"])
        return web.json_response({"ok": True, "already_paid": False})

    async def api_admin_users(request: web.Request) -> web.Response:
        settings = _settings(request)
        if not notify.is_admin(settings, _user_id(request)):
            raise ApiError(403, "Только для владельца.", "FORBIDDEN")
        users = await asyncio.to_thread(billing.list_users, settings)
        return web.json_response({"ok": True, "users": [{
            "user_id": u.user_id,
            "username": u.username,
            "first_name": u.first_name,
            "paid_until": u.paid_until,
            "paid": billing.is_paid(u.user_id),
            "notes": u.notes,
            "created_at": u.created_at,
            "projects": u.projects,
        } for u in users]})

    async def api_admin_set_paid(request: web.Request) -> web.Response:
        settings = _settings(request)
        admin_id = _user_id(request)
        if not notify.is_admin(settings, admin_id):
            raise ApiError(403, "Только для владельца.", "FORBIDDEN")
        uid = int(request.match_info["uid"])
        body = await request.json()
        if body.get("paid"):
            days = body.get("days")
            until = billing.grant_paid(uid, int(days) if days else None, notes="mini app admin")
            billing.log_event("payment_granted", f"until {until[:10]}", user_id=uid)
            notify.user_message(uid, notify.payment_granted_text(until))
            return web.json_response({"ok": True, "paid_until": until})
        billing.revoke_paid(uid)
        billing.log_event("payment_revoked", "", user_id=uid)
        notify.user_message(uid, "Доступ по подписке отключён. Напиши владельцу, если это ошибка.")
        return web.json_response({"ok": True, "paid_until": None})

    async def api_admin_events(request: web.Request) -> web.Response:
        settings = _settings(request)
        if not notify.is_admin(settings, _user_id(request)):
            raise ApiError(403, "Только для владельца.", "FORBIDDEN")
        events = billing.recent_events(80)
        usage_data = await asyncio.to_thread(_usage_totals_safe)
        return web.json_response({"ok": True, "events": events, "usage": usage_data})

    def _usage_totals_safe() -> dict[str, Any]:
        from usage import usage_totals
        return usage_totals()


def start_webserver(settings: Settings, manager: JobManager) -> None:
    """Запускает Mini App сервер в daemon-потоке. Не падает, если aiohttp не установлен."""
    if not AIOHTTP_OK:
        logger.warning("aiohttp не установлен — Mini App недоступен. Установи: pip install aiohttp")
        return
    app = web.Application(middlewares=[auth_middleware])
    app["settings"] = settings
    app["jobs"] = manager
    app.router.add_get("/", index)
    app.router.add_get("/static/{filename}", static_file)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/projects", api_projects)
    app.router.add_post("/api/projects", api_project_create)
    app.router.add_get("/api/projects/{name}", api_project_detail)
    app.router.add_post("/api/projects/{name}/upload", api_upload)
    app.router.add_post("/api/projects/{name}/materials/role", api_material_role)
    app.router.add_delete("/api/projects/{name}/materials", api_material_delete)
    app.router.add_post("/api/projects/{name}/plan", api_plan_create)
    app.router.add_put("/api/projects/{name}/plan", api_plan_put)
    app.router.add_post("/api/projects/{name}/sections/{sid}/{action}", api_section_action)
    app.router.add_post("/api/projects/{name}/chapter", api_chapter)
    app.router.add_post("/api/projects/{name}/assemble", api_assemble)
    app.router.add_get("/api/jobs", api_jobs)
    app.router.add_get("/api/jobs/{job_id}", api_job_info)
    app.router.add_post("/api/jobs/{job_id}/cancel", api_job_cancel)
    app.router.add_get("/api/download", api_download)
    app.router.add_post("/api/payment/request", api_payment_request)
    app.router.add_get("/api/admin/users", api_admin_users)
    app.router.add_post("/api/admin/users/{uid}/paid", api_admin_set_paid)
    app.router.add_get("/api/admin/events", api_admin_events)

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        runner = web.AppRunner(app)
        try:
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, settings.miniapp_host, settings.miniapp_port)
            loop.run_until_complete(site.start())
            logger.info("Mini App сервер запущен: http://%s:%s", settings.miniapp_host, settings.miniapp_port)
            loop.run_forever()
        except Exception:
            logger.exception("Mini App сервер не запустился")

    threading.Thread(target=run, daemon=True, name="miniapp-web").start()
