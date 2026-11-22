"""Small shared helpers used across PaperTrim."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable


APP_NAME = "PaperTrim"
APP_VERSION = "0.2.0"
SESSION_VERSION = 1

SUPPORTED_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"
}


class UserError(Exception):
    """An error whose message is safe and useful to show in the UI."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def timestamp_slug() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def natural_key(value: str) -> list[Any]:
    """Sort SCAN2 before SCAN10."""
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


def safe_filename(name: str, fallback: str = "image", max_len: int = 120) -> str:
    """Make a cross-platform-safe file name while keeping CJK characters."""
    name = unicodedata.normalize("NFC", str(name or "")).strip()
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if name.split(".")[0].upper() in reserved:
        name = "_" + name
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 8:
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name or fallback


def stem_of(name: str) -> str:
    base = os.path.basename(str(name))
    stem, _ext = os.path.splitext(base)
    return safe_filename(stem or base)


def unique_path(path: Path, is_dir: bool) -> Path:
    """Return path, or path_2/path_3/... if it already exists."""
    path = Path(path)
    if not path.exists():
        return path
    if is_dir:
        for i in range(2, 1000):
            candidate = path.with_name(f"{path.name}_{i}")
            if not candidate.exists():
                return candidate
    else:
        for i in range(2, 1000):
            candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
            if not candidate.exists():
                return candidate
    raise UserError("目标路径冲突过多，请换一个路径。")


def atomic_write_json(path: Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def fmt_seconds(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    return f"{seconds:.1f} s"


def walk_images(paths: Iterable[str], recursive: bool = True) -> list[Path]:
    """Expand files/directories into a sorted list of supported image paths."""
    found: list[Path] = []
    for raw in paths:
        p = Path(str(raw)).expanduser()
        if not p.exists():
            continue
        if p.is_file():
            if p.suffix.lower() in SUPPORTED_IMAGE_EXTS:
                found.append(p)
            continue
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for child in it:
                if child.is_file() and child.suffix.lower() in SUPPORTED_IMAGE_EXTS:
                    found.append(child)
    # De-duplicate by resolved path, preserving natural order.
    seen: set[str] = set()
    result: list[Path] = []
    for p in sorted(found, key=lambda q: natural_key(str(q))):
        try:
            key = str(p.resolve()).casefold()
        except OSError:
            key = str(p).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result
