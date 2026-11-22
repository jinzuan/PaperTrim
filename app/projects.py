"""Multiple local projects under one data directory.

The active project is a normal Store.  A small proxy lets the HTTP handlers
keep using the same `store` object when the user switches projects.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from .store import Store
from .util import atomic_write_json, now_iso


class StoreProxy:
    def __init__(self, store: Store):
        object.__setattr__(self, "_store", store)

    def bind(self, store: Store) -> None:
        object.__setattr__(self, "_store", store)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_store"), name)


class ProjectManager:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.projects_dir = self.root / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / "projects.json"
        self.single = False
        self.proxy = StoreProxy(None)  # type: ignore[arg-type]
        self._registry = self._load_registry()
        if not self._registry.get("projects"):
            self._migrate_or_create()
        current = self._entry(str(self._registry.get("current") or ""))
        if current is None:
            current = self._registry["projects"][0]
            self._registry["current"] = current["id"]
        self.proxy.bind(Store(self._project_dir(current["id"])))
        self._write_registry()

    @classmethod
    def from_store(cls, store: Store) -> "ProjectManager":
        """Wrap a single existing Store (used by tests / embedding)."""
        self = object.__new__(cls)
        self.root = store.data_dir
        self.projects_dir = self.root / "projects"
        self.registry_path = self.root / "projects.json"
        self.single = True
        self.proxy = StoreProxy(store)
        name = (store.session.get("project") or {}).get("name") or "未命名项目"
        self._registry = {
            "current": "local",
            "projects": [{
                "id": "local",
                "name": name,
                "created_at": (store.session.get("project") or {}).get("created_at"),
                "updated_at": (store.session.get("project") or {}).get("updated_at"),
            }],
        }
        return self

    @property
    def store(self) -> Store:
        return self.proxy._store

    def _project_dir(self, project_id: str) -> Path:
        return self.projects_dir / project_id

    def _load_registry(self) -> dict[str, Any]:
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("projects"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"current": "", "projects": []}

    def _write_registry(self) -> None:
        if self.single:
            return
        try:
            atomic_write_json(self.registry_path, self._registry)
        except OSError:
            pass

    def _entry(self, project_id: str) -> dict[str, Any] | None:
        for entry in self._registry.get("projects", []):
            if entry.get("id") == project_id:
                return entry
        return None

    def _project_name_from_dir(self, directory: Path) -> str:
        try:
            data = json.loads((directory / "session.json").read_text(encoding="utf-8"))
            return str((data.get("project") or {}).get("name") or "未命名项目")
        except (OSError, ValueError):
            return "未命名项目"

    def _migrate_or_create(self) -> None:
        legacy_session = self.root / "session.json"
        if legacy_session.exists():
            project_id = f"p-{uuid.uuid4().hex[:10]}"
            target = self._project_dir(project_id)
            target.mkdir(parents=True, exist_ok=True)
            for name in ("session.json", "sources", "previews", "exports", "tmp"):
                source = self.root / name
                if source.exists():
                    shutil.move(str(source), str(target / name))
            name = self._project_name_from_dir(target)
            entry = {
                "id": project_id,
                "name": name,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            self._registry = {"current": project_id, "projects": [entry]}
            self._write_registry()
            return
        self.create("未命名项目", write=False)

    def _sync_entry(self, store: Store | None = None) -> None:
        if self.single:
            return
        current_id = str(self._registry.get("current") or "")
        entry = self._entry(current_id)
        if entry is None:
            return
        active = store or self.store
        project = active.session.get("project") or {}
        entry["name"] = project.get("name") or entry.get("name") or "未命名项目"
        entry["updated_at"] = project.get("updated_at") or now_iso()

    def projects_view(self) -> dict[str, Any]:
        items = []
        current_id = str(self._registry.get("current") or "")
        for entry in self._registry.get("projects", []):
            project_id = str(entry.get("id") or "")
            if not project_id:
                continue
            directory = self._project_dir(project_id)
            session_path = directory / "session.json"
            name = entry.get("name") or "未命名项目"
            updated = entry.get("updated_at")
            total = ready = 0
            if project_id == current_id:
                total = len(self.store.session.get("images", []))
                ready = sum(1 for rec in self.store.session.get("images", [])
                            if rec.get("status") == "ready")
                name = (self.store.session.get("project") or {}).get("name") or name
                updated = (self.store.session.get("project") or {}).get("updated_at") or updated
            elif session_path.exists():
                try:
                    data = json.loads(session_path.read_text(encoding="utf-8"))
                    images = data.get("images") or []
                    total = len(images)
                    ready = sum(1 for rec in images if rec.get("status") == "ready")
                    name = (data.get("project") or {}).get("name") or name
                    updated = (data.get("project") or {}).get("updated_at") or updated
                except (OSError, ValueError):
                    pass
            items.append({
                "id": project_id,
                "name": name,
                "total": total,
                "ready": ready,
                "updated_at": updated,
                "current": project_id == current_id,
                "cover_url": f"/media/project-cover/{project_id}.png" if self.cover_path(project_id) else None,
            })
        items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return {"current": current_id, "projects": items}

    def cover_path(self, project_id: str) -> Path | None:
        if self.single:
            previews = self.store.previews_dir
        else:
            previews = self._project_dir(project_id) / "previews"
        if not previews.is_dir():
            return None
        files = sorted(previews.glob("*_t.png"))
        return files[0] if files else None

    def save_current(self) -> Store:
        self.store.save()
        self._sync_entry()
        self._write_registry()
        return self.store

    def create(self, name: str = "未命名项目", *, write: bool = True) -> Store:
        project_id = f"p-{uuid.uuid4().hex[:10]}"
        directory = self._project_dir(project_id)
        directory.mkdir(parents=True, exist_ok=False)
        store = Store(directory)
        store.set_project_name(name or "未命名项目")
        entry = {
            "id": project_id,
            "name": (store.session.get("project") or {}).get("name") or "未命名项目",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        self._registry.setdefault("projects", []).append(entry)
        self._registry["current"] = project_id
        self.proxy.bind(store)
        if write:
            self._write_registry()
        return store

    def open(self, project_id: str) -> Store:
        if project_id == self._registry.get("current"):
            return self.store
        entry = self._entry(project_id)
        if entry is None:
            raise KeyError("项目不存在。")
        directory = self._project_dir(project_id)
        if not (directory / "session.json").exists() and not self.single:
            raise KeyError("项目文件已丢失。")
        if not self.single:
            self.store.save()
            self._sync_entry()
            self.proxy.bind(Store(directory))
            self._registry["current"] = project_id
            self._write_registry()
        return self.store

    def rename(self, name: str) -> dict[str, Any]:
        project = self.store.set_project_name(name)
        self._sync_entry()
        self._write_registry()
        return project
