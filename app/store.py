"""Project persistence and per-image operations."""

from __future__ import annotations

import copy
import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .detector import (
    DEFAULT_ALIGN, DEFAULT_PARAMS, detect_paper, detect_paper_rect, detect_quad,
    estimate_deskew_angle, estimate_skew, normalize_align, normalize_params,
)
from .imaging import (
    DEFAULT_CURVE,
    DEFAULT_OUTPUT,
    ImageCache,
    encode_bgra,
    load_bgr,
    normalize_curve,
    normalize_output,
    read_image_size,
    render_cutout,
    result_dimensions,
    save_source_preview,
    write_bytes,
)
from .util import (
    APP_VERSION,
    SESSION_VERSION,
    SUPPORTED_IMAGE_EXTS,
    UserError,
    atomic_write_json,
    clamp,
    now_iso,
    safe_filename,
    timestamp_slug,
    unique_path,
    natural_key,
)


DEFAULT_PROJECT_NAME = "未命名项目"


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _deepcopy(value: Any) -> Any:
    return copy.deepcopy(value)


class Store:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.sources_dir = self.data_dir / "sources"
        self.previews_dir = self.data_dir / "previews"
        self.exports_dir = self.data_dir / "exports"
        self.tmp_dir = self.data_dir / "tmp"
        for directory in (self.data_dir, self.sources_dir, self.previews_dir,
                          self.exports_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        # Leftovers from an interrupted upload are safe to discard.
        try:
            for child in self.tmp_dir.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
        except OSError:
            pass

        self.session_path = self.data_dir / "session.json"
        self._lock = threading.RLock()
        self._cache = ImageCache(max_items=3)
        self.session: dict[str, Any] = self._load_session()
        self._repair_records()

    # ------------------------------------------------------------------
    # Session

    def _new_session(self) -> dict[str, Any]:
        return {
            "version": SESSION_VERSION,
            "app_version": APP_VERSION,
            "project": {
                "name": DEFAULT_PROJECT_NAME,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            },
            "output_defaults": dict(DEFAULT_OUTPUT),
            "align_edge": DEFAULT_ALIGN,
            "images": [],
        }

    def _load_session(self) -> dict[str, Any]:
        if not self.session_path.exists():
            return self._new_session()
        try:
            import json
            with open(self.session_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or not isinstance(data.get("images"), list):
                raise ValueError("bad session")
            base = self._new_session()
            base.update(data)
            base.setdefault("project", {})
            base["project"].setdefault("name", DEFAULT_PROJECT_NAME)
            base["project"].setdefault("created_at", now_iso())
            base["project"].setdefault("updated_at", now_iso())
            base["output_defaults"] = normalize_output(base.get("output_defaults"))
            base["align_edge"] = normalize_align(base.get("align_edge"))
            return base
        except Exception:
            # Keep the broken file for inspection, then start clean.
            try:
                broken = self.session_path.with_name(
                    self.session_path.name + f".broken-{timestamp_slug()}"
                )
                os.replace(self.session_path, broken)
            except OSError:
                pass
            return self._new_session()

    def _repair_records(self) -> None:
        with self._lock:
            changed = False
            seen: set[str] = set()
            images: list[dict[str, Any]] = []
            for rec in self.session.get("images", []):
                if not isinstance(rec, dict):
                    changed = True
                    continue
                ident = str(rec.get("id") or "")
                if not ident or ident in seen:
                    changed = True
                    continue
                seen.add(ident)
                rec.setdefault("name", ident)
                rec.setdefault("rotation", 0)
                rec.setdefault("fine_angle", 0.0)
                rec.setdefault("polygon", [])
                rec.setdefault("strokes", [])
                rec.setdefault("manual", False)
                rec.setdefault("rev", 1)
                rec.setdefault("params", normalize_params(None))
                rec.setdefault("status", "ready" if rec.get("polygon") else "pending")
                rec.setdefault("error", None)
                rec.setdefault("detection", None)
                rec.setdefault("size_bytes", 0)
                rec.setdefault("sha256", None)
                rec.setdefault("imported_at", now_iso())
                detection = rec.get("detection") or {}
                default_output = normalize_output(self.session.get("output_defaults"))
                before_output = rec.get("output")
                rec["output"] = normalize_output(rec.get("output"), default_output)
                if rec["output"] != before_output:
                    changed = True
                rec.setdefault("quad", detection.get("quad") or [])
                rec.setdefault("paper_rect", detection.get("paper_rect") or [])
                if not rec.get("paper_rect") and rec.get("polygon"):
                    rec["paper_rect"] = detect_paper_rect(rec.get("polygon") or [])
                rec.setdefault("quad_manual", False)
                rec.setdefault("holes", detection.get("holes") or [])
                if not isinstance(rec.get("holes"), list):
                    rec["holes"] = []
                rec["curve"] = normalize_curve(rec.get("curve"))
                rec["align_edge"] = normalize_align(rec.get("align_edge"))
                rec.setdefault("paper_color", detection.get("paper_color") or "#ffffff")
                if not isinstance(rec.get("quad"), list):
                    rec["quad"] = []
                source = self.data_dir / str(rec.get("source", ""))
                if not source.is_file():
                    rec["status"] = "error"
                    rec["error"] = "源图片文件已丢失，请重新导入。"
                rec["rotation"] = int(rec.get("rotation", 0) or 0) % 360
                if rec["rotation"] not in (0, 90, 180, 270):
                    rec["rotation"] = 0
                rec["fine_angle"] = round(float(clamp(rec.get("fine_angle", 0) or 0, -45, 45)), 2)
                rec["rev"] = int(rec.get("rev", 1) or 1)
                images.append(rec)
            if len(images) != len(self.session.get("images", [])):
                changed = True
            for index, rec in enumerate(images):
                if rec.get("order") != index:
                    rec["order"] = index
                    changed = True
            self.session["images"] = images
            if changed:
                self.save()

    def save(self) -> None:
        with self._lock:
            self.session["version"] = SESSION_VERSION
            self.session["app_version"] = APP_VERSION
            self.session["project"]["updated_at"] = now_iso()
            atomic_write_json(self.session_path, self.session)

    # ------------------------------------------------------------------
    # Lookup / views

    def find(self, image_id: str) -> dict[str, Any]:
        with self._lock:
            for rec in self.session["images"]:
                if rec.get("id") == image_id:
                    return rec
        raise UserError("图片不存在，可能已被删除。", status_code=404)

    def _snapshot(self, image_id: str) -> dict[str, Any]:
        return _deepcopy(self.find(image_id))

    def _public(self, rec: dict[str, Any]) -> dict[str, Any]:
        width, height = result_dimensions(rec) if rec.get("polygon") else (0, 0)
        ready = rec.get("status") == "ready" and bool(rec.get("polygon"))
        rev = int(rec.get("rev", 1) or 1)
        ident = rec["id"]
        detection = rec.get("detection") or {}
        return {
            "id": ident,
            "name": rec.get("name", ident),
            "width": int(rec.get("width", 0) or 0),
            "height": int(rec.get("height", 0) or 0),
            "result_width": width,
            "result_height": height,
            "size_bytes": int(rec.get("size_bytes", 0) or 0),
            "status": rec.get("status", "pending"),
            "error": rec.get("error"),
            "rotation": int(rec.get("rotation", 0) or 0),
            "fine_angle": float(rec.get("fine_angle", 0) or 0),
            "manual": bool(rec.get("manual")),
            "output": rec.get("output") or normalize_output(None),
            "align_edge": normalize_align(rec.get("align_edge")),
            "paper_color": rec.get("paper_color") or "#ffffff",
            "quad_manual": bool(rec.get("quad_manual")),
            "paper_rect": rec.get("paper_rect") or [],
            "rev": rev,
            "order": int(rec.get("order", 0) or 0),
            "imported_at": rec.get("imported_at"),
            "confidence": detection.get("confidence"),
            "warnings": detection.get("warnings") or [],
            "elapsed_ms": detection.get("elapsed_ms"),
            "thumb_url": f"/media/thumb/{ident}/{rev}.png" if ready else None,
            "preview_url": f"/media/preview/{ident}/{rev}/1600.png" if ready else None,
            "large_url": f"/media/preview/{ident}/{rev}/2400.png" if ready else None,
            "source_url": f"/media/source/{ident}.jpg",
        }

    def session_view(self) -> dict[str, Any]:
        with self._lock:
            images = [self._public(rec) for rec in self.session["images"]]
            project = _deepcopy(self.session.get("project") or {})
        counts = {"total": len(images), "ready": 0, "pending": 0, "error": 0}
        for record in images:
            status = record["status"]
            if status == "ready":
                counts["ready"] += 1
            elif status == "error":
                counts["error"] += 1
            else:
                counts["pending"] += 1
        return {
            "version": APP_VERSION,
            "session_version": SESSION_VERSION,
            "project": project,
            "images": images,
            "counts": counts,
            "defaults": {
                "detection": dict(DEFAULT_PARAMS),
                "output": _deepcopy(self.session.get("output_defaults") or DEFAULT_OUTPUT),
                "align_edge": normalize_align(self.session.get("align_edge")),
                "export_dir": str(self.exports_dir),
            },
        }

    def detail(self, image_id: str) -> dict[str, Any]:
        with self._lock:
            rec = _deepcopy(self.find(image_id))
        data = self._public(rec)
        data.update({
            "polygon": rec.get("polygon") or [],
            "paper_rect": rec.get("paper_rect") or [],
            "strokes": rec.get("strokes") or [],
            "params": rec.get("params") or normalize_params(None),
            "detection": rec.get("detection"),
            "quad": rec.get("quad") or [],
            "quad_manual": bool(rec.get("quad_manual")),
            "holes": rec.get("holes") or [],
            "curve": rec.get("curve") or normalize_curve(None),
            "source_preview_url": f"/media/source/{rec['id']}.jpg",
        })
        return data

    def set_project_name(self, name: str) -> dict[str, Any]:
        name = safe_filename(str(name or "").strip(), DEFAULT_PROJECT_NAME, max_len=80)
        with self._lock:
            self.session.setdefault("project", {})["name"] = name
            self.save()
            return _deepcopy(self.session["project"])

    def set_output_defaults(self, output: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.session["output_defaults"] = normalize_output(
                output, self.session.get("output_defaults")
            )
            self.save()
            return _deepcopy(self.session["output_defaults"])

    # ------------------------------------------------------------------
    # Import

    def import_file(
        self,
        path: str | Path,
        original_name: str | None = None,
        *,
        move: bool = False,
        dedupe: bool = True,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Copy/move one image into the project. Returns (record, skip_reason)."""
        path = Path(path)
        name = safe_filename(original_name or path.name)
        ext = path.suffix.lower()
        if ext not in SUPPORTED_IMAGE_EXTS:
            return None, "不支持的图片格式"
        if not path.is_file():
            return None, "文件不存在"
        digest = _sha256(path)
        with self._lock:
            if dedupe:
                for rec in self.session["images"]:
                    if rec.get("sha256") == digest and rec.get("status") != "error":
                        return None, "重复图片"
        try:
            width, height = read_image_size(path)
        except Exception:
            return None, "无法读取图片，文件可能已损坏"

        ident = uuid.uuid4().hex[:14]
        dest = self.sources_dir / f"{ident}{ext}"
        try:
            if move:
                shutil.move(str(path), str(dest))
            else:
                shutil.copy2(str(path), str(dest))
        except OSError as exc:
            return None, f"复制文件失败：{exc}"

        record = {
            "id": ident,
            "name": name,
            "source": f"sources/{ident}{ext}",
            "width": int(width),
            "height": int(height),
            "size_bytes": int(dest.stat().st_size),
            "sha256": digest,
            "imported_at": now_iso(),
            "order": 0,
            "status": "pending",
            "error": None,
            "rotation": 0,
            "fine_angle": 0.0,
            "polygon": [],
            "strokes": [],
            "manual": False,
            "params": normalize_params(None),
            "output": normalize_output(self.session.get("output_defaults")),
            "align_edge": normalize_align(self.session.get("align_edge")),
            "paper_rect": [],
            "quad": [],
            "quad_manual": False,
            "holes": [],
            "curve": dict(DEFAULT_CURVE),
            "paper_color": "#ffffff",
            "detection": None,
            "rev": 1,
        }
        with self._lock:
            self.session["images"].append(record)
            for index, rec in enumerate(self.session["images"]):
                rec["order"] = index
            self.save()
            return _deepcopy(record), None

    def image_path(self, rec: dict[str, Any]) -> Path:
        return self.data_dir / str(rec.get("source", ""))

    def load_source(self, rec: dict[str, Any]):
        """Decoded BGR source for a record (cached)."""
        return self._cache.get(self.image_path(rec))

    # ------------------------------------------------------------------
    # Detection

    def detect(self, image_id: str, params: dict[str, Any] | None = None,
               clear_manual: bool = True) -> dict[str, Any]:
        rec = self.find(image_id)
        source_path = self.image_path(rec)
        merged_params = normalize_params(params, rec.get("params") or {})
        try:
            image = self._cache.get(source_path)
        except UserError:
            with self._lock:
                rec = self.find(image_id)
                rec["status"] = "error"
                rec["error"] = "源图片文件已丢失，请重新导入。"
                rec["rev"] = int(rec.get("rev", 1)) + 1
                self.save()
            raise

        try:
            detection = detect_paper(image, merged_params)
        except UserError as exc:
            with self._lock:
                rec = self.find(image_id)
                rec["status"] = "error"
                rec["error"] = exc.message
                rec["params"] = merged_params
                rec["rev"] = int(rec.get("rev", 1)) + 1
                self.save()
            raise

        with self._lock:
            rec = self.find(image_id)
            rec["polygon"] = detection["polygon"]
            rec["params"] = merged_params
            rec["detection"] = detection
            rec["status"] = "ready"
            rec["error"] = None
            rec["manual"] = False
            rec["quad"] = detection.get("quad") or []
            rec["paper_rect"] = detection.get("paper_rect") or []
            rec["quad_manual"] = False
            rec["holes"] = detection.get("holes") or []
            rec["paper_color"] = detection.get("paper_color") or "#ffffff"
            if clear_manual:
                rec["strokes"] = []
            rec["rev"] = int(rec.get("rev", 1)) + 1
            self.save()
            snapshot = _deepcopy(rec)

        self._write_source_preview_if_needed(snapshot, image)
        if self._write_thumb(snapshot):
            self._cleanup_old_previews(snapshot["id"], int(snapshot["rev"]))
        return self.detail(image_id)

    def _write_source_preview_if_needed(self, rec: dict[str, Any], image) -> None:
        path = self.previews_dir / f"{rec['id']}_src.jpg"
        if not path.exists():
            try:
                save_source_preview(path, image)
            except Exception:
                pass

    def _write_thumb(self, rec: dict[str, Any]) -> bool:
        if rec.get("status") != "ready" or not rec.get("polygon"):
            return False
        try:
            image = self._cache.get(self.image_path(rec))
            bgra = render_cutout(image, rec, max_side=520)
            data = encode_bgra(bgra, "png", quality=92)
            path = self.previews_dir / f"{rec['id']}_{rec['rev']}_t.png"
            write_bytes(path, data)
            return True
        except Exception:
            # A failed thumbnail must not mark an otherwise valid crop as failed.
            return False

    def _cleanup_old_previews(self, image_id: str, keep_rev: int) -> None:
        src_name = f"{image_id}_src.jpg"
        pattern = re.compile(rf"^{re.escape(image_id)}_(\d+)_")
        try:
            for child in self.previews_dir.glob(f"{image_id}_*"):
                if child.name == src_name:
                    continue
                match = pattern.match(child.name)
                # Only remove revisions older than keep_rev.  A slow background
                # render for an older snapshot must never delete a newer preview.
                if match and int(match.group(1)) < keep_rev:
                    try:
                        child.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Editing

    def apply_edit(self, image_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise UserError("请求内容无效。")
        with self._lock:
            rec = self.find(image_id)
            if "rotation" in patch:
                try:
                    rotation = int(patch["rotation"]) % 360
                except (TypeError, ValueError):
                    raise UserError("旋转角度无效。")
                if rotation not in (0, 90, 180, 270):
                    raise UserError("旋转角度只能是 0、90、180 或 270 度。")
                rec["rotation"] = rotation
            if "align_edge" in patch:
                rec["align_edge"] = normalize_align(patch.get("align_edge"))
            if "fine_angle" in patch:
                try:
                    rec["fine_angle"] = round(float(clamp(patch["fine_angle"], -45, 45)), 2)
                except (TypeError, ValueError):
                    raise UserError("微调角度无效。")
            if "params" in patch:
                rec["params"] = normalize_params(patch.get("params"), rec.get("params") or {})
            if "output" in patch:
                rec["output"] = normalize_output(patch.get("output"), rec.get("output") or {})
            if "quad" in patch:
                rec["quad"] = self._validate_quad(patch.get("quad"), rec["width"], rec["height"])
                rec["quad_manual"] = bool(patch.get("quad_manual", True)) and bool(rec["quad"])
            if "holes" in patch:
                rec["holes"] = self._validate_holes(patch.get("holes"), rec["width"], rec["height"])
            if "curve" in patch:
                rec["curve"] = normalize_curve(patch.get("curve"), rec.get("curve") or {})
            if "paper_color" in patch:
                rec["paper_color"] = str(patch.get("paper_color") or "#ffffff")[:7].lower()
            if "polygon" in patch:
                polygon = self._validate_polygon(patch.get("polygon"), rec["width"], rec["height"])
                rec["polygon"] = polygon
                rec["manual"] = True
                rec["paper_rect"] = detect_paper_rect(polygon)
                if not rec.get("quad_manual"):
                    rec["quad"] = detect_quad(polygon)["quad"]
                if rec.get("status") != "error":
                    rec["status"] = "ready"
                    rec["error"] = None
            if "strokes" in patch:
                rec["strokes"] = self._validate_strokes(patch.get("strokes"), rec["width"], rec["height"])
                if rec["strokes"]:
                    rec["manual"] = True
            if patch.get("manual") is False and "polygon" not in patch and "strokes" not in patch:
                rec["manual"] = False
            if "name" in patch:
                rec["name"] = safe_filename(patch.get("name"), rec.get("name") or rec["id"])
            if not rec.get("polygon"):
                raise UserError("尚未识别出裁切轮廓，请先重新识别。")
            rec["rev"] = int(rec.get("rev", 1)) + 1
            self.save()
            snapshot = _deepcopy(rec)

        if self._write_thumb(snapshot):
            self._cleanup_old_previews(snapshot["id"], int(snapshot["rev"]))
        return self.detail(image_id)

    def _validate_holes(self, holes: Any, width: int, height: int) -> list[list[list[float]]]:
        if holes in (None, []):
            return []
        if not isinstance(holes, list):
            raise UserError("扣洞轮廓格式无效。")
        result: list[list[list[float]]] = []
        for hole in holes[:200]:
            try:
                points = self._validate_polygon(hole, width, height)
            except UserError:
                continue
            if len(points) >= 3:
                result.append(points)
        return result

    def _validate_quad(self, quad: Any, width: int, height: int) -> list[list[float]]:
        if quad is None or quad == []:
            return []
        if not isinstance(quad, list) or len(quad) != 4:
            raise UserError("四角坐标需要正好 4 个点。")
        result: list[list[float]] = []
        for point in quad:
            try:
                x, y = float(point[0]), float(point[1])
            except (TypeError, IndexError, ValueError):
                raise UserError("四角坐标格式无效。")
            result.append([
                round(clamp(x, 0, width - 1), 1),
                round(clamp(y, 0, height - 1), 1),
            ])
        return result

    def _validate_polygon(self, polygon: Any, width: int, height: int) -> list[list[float]]:
        if not isinstance(polygon, list):
            raise UserError("裁切轮廓格式无效。")
        if len(polygon) > 20000:
            raise UserError("裁切轮廓点过多。")
        result: list[list[float]] = []
        for point in polygon:
            try:
                x, y = float(point[0]), float(point[1])
            except (TypeError, IndexError, ValueError):
                continue
            result.append([
                round(clamp(x, 0, width - 1), 1),
                round(clamp(y, 0, height - 1), 1),
            ])
        if len(result) < 3:
            raise UserError("裁切轮廓至少需要 3 个点。")
        return result

    def _validate_strokes(self, strokes: Any, width: int, height: int) -> list[dict[str, Any]]:
        if strokes is None:
            return []
        if not isinstance(strokes, list):
            raise UserError("笔迹格式无效。")
        if len(strokes) > 500:
            raise UserError("手动笔迹过多，请先清理一些。")
        result: list[dict[str, Any]] = []
        for stroke in strokes:
            if not isinstance(stroke, dict):
                continue
            points = stroke.get("points")
            if not isinstance(points, list) or not points:
                continue
            if len(points) > 5000:
                points = points[:5000]
            clean_points = []
            for point in points:
                try:
                    x, y = float(point[0]), float(point[1])
                except (TypeError, IndexError, ValueError):
                    continue
                clean_points.append([
                    round(clamp(x, 0, width - 1), 1),
                    round(clamp(y, 0, height - 1), 1),
                ])
            if not clean_points:
                continue
            result.append({
                "mode": "erase" if stroke.get("mode") == "erase" else "add",
                "radius": int(clamp(float(stroke.get("radius", 24)), 1, 500)),
                "points": clean_points,
            })
        return result

    def deskew(self, image_id: str, force: bool = True) -> dict[str, Any]:
        rec = self.find(image_id)
        align = normalize_align(rec.get("align_edge"))
        try:
            estimate = estimate_skew(
                self.load_source(rec), rec.get("polygon") or [], align=align,
            )
            angle = estimate["angle"]
        except Exception:
            angle = None
        if angle is None:
            angle = estimate_deskew_angle(rec.get("polygon") or [])
        angle = round(float(clamp(angle, -45, 45)), 2)
        if not force:
            # The outline estimator is edge-weighted and is reliable even
            # when no text/panel lines exist.  Skipping sub-2° outline angles
            # used to leave scans visibly tilted (~1° over a 3000 px page is
            # ~50 px), so only ignore truly negligible corrections.
            if abs(angle) < 0.12:
                return self.detail(image_id)
        if abs(float(rec.get("fine_angle", 0) or 0) - angle) < 0.05:
            return self.detail(image_id)
        return self.apply_edit(image_id, {"fine_angle": angle})

    def align_edge(self, image_id: str, align: str) -> dict[str, Any]:
        align = normalize_align(align)
        with self._lock:
            rec = self.find(image_id)
            rec["align_edge"] = align
            self.save()
        return self.deskew(image_id, force=True)

    def set_align_default(self, align: str) -> str:
        with self._lock:
            self.session["align_edge"] = normalize_align(align)
            self.save()
            return self.session["align_edge"]

    def set_rotation(self, image_id: str, rotation: int) -> dict[str, Any]:
        return self.apply_edit(image_id, {"rotation": rotation})

    # ------------------------------------------------------------------
    # Deletion / maintenance

    def remove(self, image_id: str) -> None:
        with self._lock:
            rec = self.find(image_id)
            self.session["images"] = [
                item for item in self.session["images"] if item.get("id") != image_id
            ]
            for index, item in enumerate(self.session["images"]):
                item["order"] = index
            self.save()
        source = self.data_dir / str(rec.get("source", ""))
        try:
            if source.is_file():
                source.unlink()
        except OSError:
            pass
        try:
            for child in self.previews_dir.glob(f"{image_id}_*"):
                child.unlink(missing_ok=True)
        except OSError:
            pass

    def clear_all(self) -> None:
        with self._lock:
            self.session["images"] = []
            self.save()
        for directory in (self.sources_dir, self.previews_dir):
            try:
                shutil.rmtree(directory)
            except OSError:
                pass
            directory.mkdir(parents=True, exist_ok=True)

    def sort_by_name(self) -> None:
        with self._lock:
            self.session["images"].sort(
                key=lambda rec: natural_key(str(rec.get("name") or ""))
            )
            for index, rec in enumerate(self.session["images"]):
                rec["order"] = index
            self.save()

    def image_paths(self, image_ids: list[str]) -> list[dict[str, Any]]:
        wanted = set(image_ids)
        result = []
        with self._lock:
            for rec in self.session["images"]:
                if rec["id"] in wanted:
                    result.append(_deepcopy(rec))
        return result

    def all_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_deepcopy(rec) for rec in self.session["images"]]

    # ------------------------------------------------------------------
    # Preview files

    def ensure_source_preview(self, image_id: str) -> Path:
        rec = self.find(image_id)
        path = self.previews_dir / f"{image_id}_src.jpg"
        if path.exists():
            return path
        image = self._cache.get(self.image_path(rec))
        save_source_preview(path, image)
        return path

    def ensure_thumb(self, image_id: str, rev: int) -> Path:
        rec = self._snapshot(image_id)
        if int(rec.get("rev", 0)) != int(rev) or rec.get("status") != "ready":
            raise UserError("缩略图已过期，请刷新。", status_code=404)
        path = self.previews_dir / f"{image_id}_{rev}_t.png"
        if not path.exists():
            self._write_thumb(rec)
        if not path.exists():
            raise UserError("缩略图生成失败。", status_code=404)
        return path

    def ensure_preview(self, image_id: str, rev: int, size: int) -> Path:
        rec = self._snapshot(image_id)
        if int(rec.get("rev", 0)) != int(rev):
            raise UserError("预览已过期，请刷新。", status_code=404)
        if rec.get("status") != "ready" or not rec.get("polygon"):
            raise UserError("图片尚未识别完成。", status_code=409)
        size = int(clamp(size, 200, 2600))
        path = self.previews_dir / f"{image_id}_{rev}_v{size}.png"
        if not path.exists():
            image = self._cache.get(self.image_path(rec))
            bgra = render_cutout(image, rec, max_side=size)
            write_bytes(path, encode_bgra(bgra, "png", quality=92))
        return path

    # ------------------------------------------------------------------
    # Export paths

    def default_export_path(self, kind: str) -> Path:
        project_name = safe_filename(
            (self.session.get("project") or {}).get("name") or DEFAULT_PROJECT_NAME,
            "papertrim", max_len=40,
        )
        stamp = timestamp_slug()
        if kind == "folder":
            name = f"{project_name}_{stamp}_裁切图"
            return unique_path(self.exports_dir / name, is_dir=True)
        if kind == "pdf":
            return unique_path(self.exports_dir / f"{project_name}_{stamp}.pdf", is_dir=False)
        if kind == "long":
            return unique_path(self.exports_dir / f"{project_name}_{stamp}_长图.jpg", is_dir=False)
        if kind == "zip":
            return unique_path(self.exports_dir / f"{project_name}_{stamp}_裁切图.zip", is_dir=False)
        return self.exports_dir / f"{project_name}_{stamp}"

    def resolve_export_path(self, raw: str | None, kind: str) -> Path:
        if not raw or not str(raw).strip():
            return self.default_export_path(kind)
        path = Path(os.path.expandvars(os.path.expanduser(str(raw).strip())))
        if not path.is_absolute():
            path = self.exports_dir / path
        if kind == "folder":
            return path
        suffix = { "pdf": ".pdf", "zip": ".zip", "long": ".jpg" }.get(kind)
        if suffix and path.suffix.lower() != suffix:
            path = path.with_suffix(suffix)
        return path
