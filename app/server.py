"""Local HTTP API for PaperTrim.

The server only listens on loopback, validates the Host header (DNS-rebinding
protection) and requires a per-run bearer token for every /api request.  The
token is injected into the HTML shell; a web page from another origin cannot
read it.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy as _deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .accelerate import configure as configure_performance
from .detector import DEFAULT_PARAMS, normalize_align, normalize_params
from .exporters import run_export
from .imaging import encode_bgra, normalize_output, render_editor_preview, render_record
from .jobs import JobManager
from .projects import ProjectManager
from .store import Store
from .util import (
    APP_NAME,
    APP_VERSION,
    SUPPORTED_IMAGE_EXTS,
    UserError,
    natural_key,
    safe_filename,
    timestamp_slug,
    walk_images,
)


log = logging.getLogger("papertrim")
if getattr(sys, "frozen", False):
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


def _json(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code)


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise UserError("请求内容不是有效的 JSON。") from exc
    if not isinstance(data, dict):
        raise UserError("请求内容格式无效。")
    return data


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def create_app(store: Store | ProjectManager, jobs: JobManager, token: str | None = None,
               port: int | None = None) -> Starlette:
    token = token or secrets.token_urlsafe(24)
    performance = configure_performance()
    manager = store if isinstance(store, ProjectManager) else ProjectManager.from_store(store)
    store = manager.proxy
    allowed_hosts = {"127.0.0.1", "localhost", "::1", "testserver"}

    async def root(request: Request) -> Response:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        nonce = secrets.token_urlsafe(16)
        bootstrap = json.dumps(
            {"token": token, "version": APP_VERSION, "app": APP_NAME},
            ensure_ascii=False,
        ).replace("</", "<\\/")
        html = (
            html.replace("__PAPERTRIM_BOOTSTRAP__", bootstrap)
            .replace("__PAPERTRIM_NONCE__", nonce)
            .replace("__PAPERTRIM_VERSION__", APP_VERSION)
        )
        response = HTMLResponse(html)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def health(_request: Request) -> Response:
        return PlainTextResponse(f"{APP_NAME} {APP_VERSION}")

    async def api_session(_request: Request) -> Response:
        data = store.session_view()
        data["jobs"] = jobs.active()
        data["performance"] = performance
        return _json(data)

    async def api_project(request: Request) -> Response:
        body = await _body(request)
        project = manager.rename(str(body.get("name", "")))
        return _json({"project": project})

    async def api_project_save(request: Request) -> Response:
        manager.save_current()
        return _json({"ok": True, "project": store.session_view().get("project")})

    async def api_projects(request: Request) -> Response:
        return _json(manager.projects_view())

    async def api_project_new(request: Request) -> Response:
        if jobs.active():
            raise UserError("请等待当前任务完成后再新建项目。")
        body = await _body(request)
        manager.create(str(body.get("name") or "未命名项目"))
        return _json({"session": store.session_view(), "projects": manager.projects_view()})

    async def api_project_open(request: Request) -> Response:
        if jobs.active():
            raise UserError("请等待当前任务完成后再切换项目。")
        body = await _body(request)
        try:
            manager.open(str(body.get("id") or ""))
        except KeyError as exc:
            raise UserError(str(exc.args[0]) if exc.args else "项目不存在。")
        return _json({"session": store.session_view(), "projects": manager.projects_view()})

    async def media_project_cover(request: Request) -> Response:
        path = manager.cover_path(request.path_params["project_id"])
        if path is None or not path.is_file():
            raise UserError("这个项目还没有封面。")
        return FileResponse(path, media_type="image/png")

    async def api_output_defaults(request: Request) -> Response:
        body = await _body(request)
        output = body.get("output") if isinstance(body.get("output"), dict) else body
        return _json({"output": store.set_output_defaults(output)})

    async def api_image(request: Request) -> Response:
        image_id = request.path_params["image_id"]
        return _json(store.detail(image_id))

    async def api_image_patch(request: Request) -> Response:
        image_id = request.path_params["image_id"]
        body = await _body(request)
        return _json(store.apply_edit(image_id, body))

    async def api_image_delete(request: Request) -> Response:
        image_id = request.path_params["image_id"]
        store.remove(image_id)
        return _json({"ok": True, "session": store.session_view()})

    async def api_recompute(request: Request) -> Response:
        image_id = request.path_params["image_id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        params = body.get("params")
        store.detect(image_id, params, clear_manual=True)
        store.deskew(image_id, force=False)
        return _json(store.detail(image_id))

    async def api_deskew(request: Request) -> Response:
        image_id = request.path_params["image_id"]
        return _json(store.deskew(image_id))

    async def api_align(request: Request) -> Response:
        image_id = request.path_params["image_id"]
        body = await _body(request)
        align = normalize_align(body.get("align"))
        return _json(store.align_edge(image_id, align))

    async def api_render_preview(request: Request) -> Response:
        image_id = request.path_params["image_id"]
        body = await _body(request)
        output = body.get("output") if isinstance(body.get("output"), dict) else {}
        rec = _deepcopy(store.find(image_id))
        if rec.get("status") != "ready" or not rec.get("polygon"):
            raise UserError("这张图片还没有完成识别。")
        rec["output"] = normalize_output(output, rec.get("output") or {})
        source = store.load_source(rec)
        try:
            max_side = int(body.get("max_side") or 1100)
        except (TypeError, ValueError):
            max_side = 1100
        max_side = max(300, min(2400, max_side))

        def render() -> bytes:
            if body.get("overlay"):
                bgra = render_editor_preview(source, rec, max_side=max_side)
            else:
                bgra = render_record(source, rec, max_side=max_side)
            return encode_bgra(bgra, "png", background=None, dpi=72, png_optimize=True)

        data = await run_in_threadpool(render)
        return Response(
            data,
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    # ------------------------------------------------------------------
    # Import

    def start_detect_job(image_ids: list[str], params: dict[str, Any] | None,
                         deskew: bool) -> dict[str, Any]:
        def worker(handle):
            failed: list[dict[str, str]] = []
            total = len(image_ids)
            if not image_ids:
                return {"imported": 0, "failed": failed}

            def process(image_id: str) -> tuple[str, str, str | None]:
                name = image_id
                try:
                    rec = store.find(image_id)
                    name = rec.get("name") or image_id
                    store.detect(image_id, params, clear_manual=True)
                    if deskew:
                        store.deskew(image_id, force=False)
                    return image_id, name, None
                except Exception as exc:  # noqa: BLE001 - report per image
                    return image_id, name, str(exc)

            workers = max(1, min(4, int(os.cpu_count() or 1), total))
            done = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(process, image_id) for image_id in image_ids]
                for future in as_completed(futures):
                    image_id, name, error = future.result()
                    if error:
                        failed.append({"id": image_id, "name": name, "error": error})
                    done += 1
                    handle.progress(done, total, f"正在识别 {done}/{total}")
            return {"imported": len(image_ids) - len(failed), "failed": failed}

        return jobs.start("detect", len(image_ids), worker)

    async def api_import(request: Request) -> Response:
        content_length = int(request.headers.get("content-length") or 0)
        if content_length > 2 * 1024 * 1024 * 1024:
            raise UserError("一次上传的内容过大。")
        form = await request.form(max_files=500, max_fields=30)
        params: dict[str, Any] | None = None
        raw_params = form.get("params")
        if raw_params:
            try:
                parsed = json.loads(str(raw_params))
                if isinstance(parsed, dict):
                    params = parsed
            except ValueError:
                params = None
        dedupe = _parse_bool(form.get("dedupe"), True)
        deskew = _parse_bool(form.get("deskew"), True)
        uploads = form.getlist("files")
        imported: list[str] = []
        skipped: list[dict[str, str]] = []
        try:
            for upload in uploads:
                filename = safe_filename(getattr(upload, "filename", "") or "image.jpg")
                ext = Path(filename).suffix.lower()
                if ext not in SUPPORTED_IMAGE_EXTS:
                    skipped.append({"name": filename, "reason": "不支持的图片格式"})
                    try:
                        await upload.close()
                    except Exception:
                        pass
                    continue
                temp = store.tmp_dir / f"upload_{uuid.uuid4().hex}{ext}"
                try:
                    with open(temp, "wb") as fh:
                        while True:
                            chunk = await upload.read(1024 * 1024)
                            if not chunk:
                                break
                            fh.write(chunk)
                finally:
                    try:
                        await upload.close()
                    except Exception:
                        pass
                record, reason = store.import_file(
                    temp, original_name=filename, move=True, dedupe=dedupe
                )
                if record is None:
                    skipped.append({"name": filename, "reason": reason or "已跳过"})
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass
                else:
                    imported.append(record["id"])
        finally:
            await form.close()

        job = start_detect_job(imported, params, deskew)
        return _json({
            "job": job,
            "session": store.session_view(),
            "imported": imported,
            "skipped": skipped,
        })

    async def api_import_paths(request: Request) -> Response:
        body = await _body(request)
        raw_paths = body.get("paths") or []
        if isinstance(raw_paths, str):
            raw_paths = [p for p in raw_paths.splitlines() if p.strip()]
        if not isinstance(raw_paths, list):
            raise UserError("路径列表格式无效。")
        paths = [str(p) for p in raw_paths if str(p).strip()]
        if not paths:
            raise UserError("请填写至少一个文件或文件夹路径。")
        recursive = _parse_bool(body.get("recursive"), True)
        dedupe = _parse_bool(body.get("dedupe"), True)
        deskew = _parse_bool(body.get("deskew"), True)
        params = body.get("params") if isinstance(body.get("params"), dict) else None
        files = walk_images(paths, recursive=recursive)
        if not files:
            raise UserError("这些路径下没有找到支持的图片（JPG/PNG/WebP/TIFF/BMP）。")
        if len(files) > 1000:
            raise UserError("一次最多导入 1000 张图片，请分批导入。")
        imported: list[str] = []
        skipped: list[dict[str, str]] = []
        for file_path in files:
            record, reason = store.import_file(
                file_path, original_name=file_path.name, move=False, dedupe=dedupe
            )
            if record is None:
                skipped.append({"name": file_path.name, "reason": reason or "已跳过"})
            else:
                imported.append(record["id"])
        job = start_detect_job(imported, params, deskew)
        return _json({
            "job": job,
            "session": store.session_view(),
            "imported": imported,
            "skipped": skipped,
            "found": len(files),
        })

    # ------------------------------------------------------------------
    # Batch

    async def api_batch(request: Request) -> Response:
        body = await _body(request)
        action = str(body.get("action") or "")
        raw_ids = body.get("ids") or []
        if not isinstance(raw_ids, list):
            raise UserError("图片列表格式无效。")
        ids: list[str] = []
        for item in raw_ids:
            ident = str(item)
            if ident and ident not in ids:
                ids.append(ident)
        if not ids:
            raise UserError("请先选择图片。")
        records = store.image_paths(ids)
        if not records:
            raise UserError("没有找到选中的图片。")
        if action == "output":
            records = [record for record in records
                       if record.get("status") == "ready" and record.get("polygon")]
            if not records:
                raise UserError("选中的图片还没有识别完成。")
        ids = [record["id"] for record in records]

        if action == "delete":
            for image_id in ids:
                store.remove(image_id)
            return _json({"ok": True, "deleted": len(ids), "session": store.session_view()})

        params = body.get("params") if isinstance(body.get("params"), dict) else None
        output = body.get("output") if isinstance(body.get("output"), dict) else None
        remember = _parse_bool(body.get("remember"), False)
        align_value = normalize_align(body.get("align")) if body.get("align") else None
        edge_value = body.get("edge")
        try:
            edge_value = int(edge_value) if edge_value is not None else None
        except (TypeError, ValueError):
            edge_value = None
        try:
            value = int(body.get("value") or 0)
        except (TypeError, ValueError):
            value = 0
        if action == "output":
            if not output:
                raise UserError("缺少输出设置。")
            if remember:
                store.set_output_defaults(output)
                if align_value:
                    store.set_align_default(align_value)

        def worker(handle):
            failed: list[dict[str, str]] = []
            total = len(ids)
            for index, image_id in enumerate(ids):
                name = image_id
                try:
                    rec = store.find(image_id)
                    name = rec.get("name") or image_id
                    if action == "rotate":
                        current = int(rec.get("rotation", 0) or 0)
                        store.set_rotation(image_id, current + value)
                    elif action == "set_rotation":
                        store.set_rotation(image_id, value)
                    elif action == "deskew":
                        store.deskew(image_id)
                    elif action == "recompute":
                        store.detect(image_id, params, clear_manual=True)
                        store.deskew(image_id, force=False)
                    elif action == "reset":
                        store.detect(image_id, dict(DEFAULT_PARAMS), clear_manual=True)
                        store.deskew(image_id, force=False)
                    elif action == "output":
                        edit = {"output": output}
                        if align_value:
                            edit["align_edge"] = align_value
                        if edge_value is not None:
                            params_now = dict(store.find(image_id).get("params") or {})
                            params_now["expand"] = edge_value
                            edit["params"] = params_now
                        store.apply_edit(image_id, edit)
                        if align_value:
                            store.deskew(image_id, force=True)
                    else:
                        raise UserError("未知的批量操作。")
                except Exception as exc:  # noqa: BLE001
                    failed.append({"id": image_id, "name": name, "error": str(exc)})
                handle.progress(index + 1, total, f"正在处理 {index + 1}/{total}")
            return {"done": len(ids) - len(failed), "failed": failed}

        job = jobs.start(f"batch-{action}", len(ids), worker)
        return _json({"job": job, "session": store.session_view()})

    # ------------------------------------------------------------------
    # Export

    async def api_export_default(request: Request) -> Response:
        kind = str(request.query_params.get("kind") or "folder")
        path = store.default_export_path(kind)
        return _json({"path": str(path), "kind": kind})

    async def api_export(request: Request) -> Response:
        body = await _body(request)
        kind = str(body.get("kind") or "")
        if kind not in {"folder", "pdf", "long", "zip"}:
            raise UserError("未知的导出类型。")
        options = body.get("options") if isinstance(body.get("options"), dict) else {}
        use_all = _parse_bool(body.get("all"), False)
        raw_ids = body.get("ids") if isinstance(body.get("ids"), list) else []
        id_set = {str(i) for i in raw_ids}
        all_records = store.all_records()
        if use_all:
            records = all_records
        else:
            records = [rec for rec in all_records if rec["id"] in id_set]
        records = [rec for rec in records if rec.get("status") == "ready" and rec.get("polygon")]
        if not records:
            raise UserError("没有可导出的图片，请先完成识别。")

        def worker(handle):
            return run_export(store, records, kind, options, handle.progress)

        job = jobs.start(f"export-{kind}", len(records) + 1, worker)
        return _json({"job": job, "count": len(records)})

    # ------------------------------------------------------------------
    # Jobs / utilities

    async def api_job(request: Request) -> Response:
        return _json(jobs.get(request.path_params["job_id"]))

    async def api_open(request: Request) -> Response:
        body = await _body(request)
        raw = str(body.get("path") or "").strip()
        if not raw:
            raise UserError("路径为空。")
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.exists():
            raise UserError("路径不存在。")
        select = _parse_bool(body.get("select"), False)
        try:
            if sys.platform.startswith("win"):
                if select and path.is_file():
                    subprocess.Popen(["explorer", "/select,", str(path)])
                else:
                    os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            raise UserError(f"无法打开路径：{exc}")
        return _json({"ok": True})

    async def api_clear(request: Request) -> Response:
        body = await _body(request)
        if not _parse_bool(body.get("confirm"), False):
            raise UserError("需要确认后才能清空。")
        store.clear_all()
        return _json({"ok": True, "session": store.session_view()})

    async def api_sort(_request: Request) -> Response:
        store.sort_by_name()
        return _json({"ok": True, "session": store.session_view()})

    # ------------------------------------------------------------------
    # Media

    def safe_media(path: Path, media_type: str, cache: str = "private, max-age=31536000, immutable") -> Response:
        if not path.is_file():
            raise UserError("文件不存在。", status_code=404)
        response = FileResponse(path, media_type=media_type)
        response.headers["Cache-Control"] = cache
        return response

    async def media_source(request: Request) -> Response:
        path = store.ensure_source_preview(request.path_params["image_id"])
        return safe_media(path, "image/jpeg")

    async def media_thumb(request: Request) -> Response:
        path = store.ensure_thumb(
            request.path_params["image_id"],
            int(request.path_params["rev"]),
        )
        return safe_media(path, "image/png")

    async def media_preview(request: Request) -> Response:
        path = store.ensure_preview(
            request.path_params["image_id"],
            int(request.path_params["rev"]),
            int(request.path_params["size"]),
        )
        return safe_media(path, "image/png")

    async def api_404(_request: Request) -> Response:
        raise UserError("接口不存在。", status_code=404)

    routes = [
        Route("/", root),
        Route("/health", health),
        Route("/api/session", api_session),
        Route("/api/project", api_project, methods=["POST"]),
        Route("/api/project/save", api_project_save, methods=["POST"]),
        Route("/api/projects", api_projects),
        Route("/api/projects/new", api_project_new, methods=["POST"]),
        Route("/api/projects/open", api_project_open, methods=["POST"]),
        Route("/media/project-cover/{project_id}.png", media_project_cover),
        Route("/api/output-defaults", api_output_defaults, methods=["POST"]),
        Route("/api/import", api_import, methods=["POST"]),
        Route("/api/import/paths", api_import_paths, methods=["POST"]),
        Route("/api/batch", api_batch, methods=["POST"]),
        Route("/api/clear", api_clear, methods=["POST"]),
        Route("/api/sort", api_sort, methods=["POST"]),
        Route("/api/export/default", api_export_default),
        Route("/api/export", api_export, methods=["POST"]),
        Route("/api/open", api_open, methods=["POST"]),
        Route("/api/jobs/{job_id}", api_job),
        Route("/api/images/{image_id}", api_image),
        Route("/api/images/{image_id}", api_image_patch, methods=["PATCH"]),
        Route("/api/images/{image_id}", api_image_delete, methods=["DELETE"]),
        Route("/api/images/{image_id}/recompute", api_recompute, methods=["POST"]),
        Route("/api/images/{image_id}/deskew", api_deskew, methods=["POST"]),
        Route("/api/images/{image_id}/align", api_align, methods=["POST"]),
        Route("/api/images/{image_id}/render-preview", api_render_preview, methods=["POST"]),
        Route("/media/source/{image_id}.jpg", media_source),
        Route("/media/thumb/{image_id}/{rev}.png", media_thumb),
        Route("/media/preview/{image_id}/{rev}/{size}.png", media_preview),
        Mount("/static", app=StaticFiles(directory=WEB_DIR), name="static"),
        Route("/favicon.svg", lambda request: FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")),
        Route("/favicon.ico", lambda request: FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")),
        Route("/{path:path}", api_404),
    ]

    app = Starlette(routes=routes, debug=False)

    async def security_middleware(request: Request, call_next):
        hostname = request.url.hostname or ""
        if hostname not in allowed_hosts:
            return _json({"error": "Host 不被允许。"}, 403)
        path = request.url.path
        if path.startswith("/api/"):
            supplied = request.headers.get("x-papertrim-token") or ""
            if not hmac.compare_digest(supplied, token):
                return _json({"error": "会话令牌无效，请刷新页面后重试。"}, 403)
        try:
            response = await call_next(request)
        except UserError as exc:
            response = _json({"error": exc.message}, exc.status_code)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=security_middleware)

    async def user_error_handler(request: Request, exc: UserError):
        return _json({"error": exc.message}, exc.status_code)

    async def unhandled_error_handler(request: Request, exc: Exception):
        log.exception("Unhandled error on %s", request.url.path)
        return _json({"error": "服务器内部错误，请查看启动窗口中的日志。"}, 500)

    app.add_exception_handler(UserError, user_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    app.state.store = store
    app.state.jobs = jobs
    app.state.token = token
    app.state.port = port
    app.state.performance = performance
    app.state.project_manager = manager
    return app
