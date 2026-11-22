"""Tiny in-process job runner used by long imports, batch edits and exports."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from .util import UserError, now_iso


class JobHandle:
    def __init__(self, job_id: str):
        self.id = job_id
        self._progress: Callable[[int, int, str], None] | None = None

    def progress(self, done: int, total: int, message: str = "") -> None:
        if self._progress:
            self._progress(done, total, message)


class JobManager:
    def __init__(self, keep: int = 60):
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._keep = max(10, int(keep))

    def start(self, kind: str, total: int,
              worker: Callable[[JobHandle], Any]) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:16]
        job = {
            "id": job_id,
            "kind": kind,
            "status": "running",
            "total": max(0, int(total)),
            "done": 0,
            "message": "准备中…",
            "result": None,
            "error": None,
            "started_at": now_iso(),
            "finished_at": None,
        }
        handle = JobHandle(job_id)
        with self._lock:
            self._jobs[job_id] = job
            self._prune_locked()

        def progress(done: int, total_now: int, message: str = "") -> None:
            with self._lock:
                item = self._jobs.get(job_id)
                if not item:
                    return
                item["done"] = max(0, int(done))
                if total_now:
                    item["total"] = max(0, int(total_now))
                if message:
                    item["message"] = message

        handle._progress = progress

        def runner() -> None:
            try:
                result = worker(handle)
                with self._lock:
                    item = self._jobs.get(job_id)
                    if not item:
                        return
                    item["status"] = "done"
                    item["done"] = item["total"]
                    item["message"] = "完成"
                    item["result"] = result
                    item["finished_at"] = now_iso()
            except UserError as exc:
                self._fail(job_id, exc.message)
            except Exception as exc:  # noqa: BLE001 - surface a useful local error
                self._fail(job_id, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=runner, name=f"papertrim-{kind}-{job_id}",
                         daemon=True).start()
        return self.get(job_id)

    def _fail(self, job_id: str, message: str) -> None:
        with self._lock:
            item = self._jobs.get(job_id)
            if not item:
                return
            item["status"] = "error"
            item["error"] = message
            item["message"] = "失败"
            item["finished_at"] = now_iso()

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._jobs.get(job_id)
            if not item:
                raise UserError("任务不存在或已过期。", status_code=404)
            return dict(item)

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(item) for item in self._jobs.values()
                if item.get("status") == "running"
            ]

    def _prune_locked(self) -> None:
        if len(self._jobs) <= self._keep:
            return
        finished = [
            (item.get("finished_at") or "", job_id)
            for job_id, item in self._jobs.items()
            if item.get("status") != "running"
        ]
        finished.sort()
        for _stamp, job_id in finished[: max(0, len(self._jobs) - self._keep)]:
            self._jobs.pop(job_id, None)
