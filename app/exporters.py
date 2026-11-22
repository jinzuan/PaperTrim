"""Export selected crops as files, PDF, a long image or a ZIP archive."""

from __future__ import annotations

import csv
import io
import os
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
from PIL import Image

from .imaging import (
    composite_bgra,
    encode_bgra,
    parse_background,
    render_cutout,
)
from .util import UserError, clamp, safe_filename, stem_of, unique_path


Progress = Callable[[int, int, str], None]


def _noop(_done: int, _total: int, _message: str) -> None:
    pass


def _option(options: dict[str, Any], key: str, default: Any) -> Any:
    value = options.get(key, default)
    return default if value is None or value == "" else value


def _format(value: Any) -> str:
    fmt = str(value or "png").lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in {"png", "jpg", "webp", "tif", "tiff"}:
        raise UserError(f"不支持的图片格式：{value}")
    if fmt == "tif":
        fmt = "tiff"
    return fmt


def _quality(value: Any, default: int = 92) -> int:
    try:
        return int(clamp(float(value), 30, 100))
    except (TypeError, ValueError):
        return default


def _background(options: dict[str, Any], default: str = "white"):
    try:
        return parse_background(_option(options, "background", default))
    except UserError:
        return parse_background(default)


def _dpi(value: Any, default: int = 300) -> int:
    try:
        return int(clamp(float(value), 72, 600))
    except (TypeError, ValueError):
        return default


def _output_names(records: list[dict[str, Any]], fmt: str, options: dict[str, Any]) -> list[str]:
    numbering = bool(_option(options, "numbering", True))
    prefix = safe_filename(str(_option(options, "prefix", "") or ""), "", max_len=40)
    start = 1
    try:
        start = int(_option(options, "start", 1))
    except (TypeError, ValueError):
        start = 1
    width = max(1, len(str(start + len(records) - 1)))
    names: list[str] = []
    used: set[str] = set()
    for index, record in enumerate(records):
        stem = stem_of(record.get("name") or record.get("id") or "image")
        if numbering:
            base = f"{start + index:0{width}d}_{stem}"
        else:
            base = stem
        if prefix:
            base = f"{prefix}{base}"
        base = safe_filename(base, f"image_{index + 1}")
        candidate = f"{base}.{fmt}"
        counter = 2
        while candidate.casefold() in used:
            candidate = f"{base}_{counter}.{fmt}"
            counter += 1
        used.add(candidate.casefold())
        names.append(candidate)
    return names


def _manifest_bytes(rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([
        "index", "output", "source", "width", "height",
        "rotation_deg", "fine_angle_deg", "manual",
    ])
    for row in rows:
        writer.writerow([
            row["index"], row["output"], row["source"], row["width"],
            row["height"], row["rotation"], row["fine_angle"],
            "yes" if row["manual"] else "no",
        ])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _render(store, record: dict[str, Any], background=None, max_side: int | None = None):
    image = store.load_source(record)
    return render_cutout(image, record, max_side=max_side, background=background)


def _max_side(options: dict[str, Any]) -> int | None:
    try:
        value = int(float(_option(options, "max_side", 0) or 0))
    except (TypeError, ValueError):
        value = 0
    return value if value >= 200 else None


def _source_stem(record: dict[str, Any]) -> str:
    name = str(record.get("name") or "").strip()
    return name or "image"


# ---------------------------------------------------------------------------
# Folder / ZIP


def _write_image_set(
    store,
    records: list[dict[str, Any]],
    options: dict[str, Any],
    progress: Progress,
    *,
    zip_path: Path | None = None,
    folder: Path | None = None,
) -> tuple[list[dict[str, Any]], int]:
    fmt = _format(_option(options, "format", "png"))
    quality = _quality(_option(options, "quality", 92))
    background = _background(options, "transparent" if fmt in {"png", "webp"} else "white")
    dpi = _dpi(_option(options, "dpi", 300))
    max_side = _max_side(options)
    png_optimize = bool(options.get("png_optimize", False))
    png_quantize = bool(options.get("png_quantize", False))
    names = _output_names(records, fmt, options)
    manifest: list[dict[str, Any]] = []
    total = len(records) + (1 if options.get("include_manifest", True) else 0)
    done = 0
    total_bytes = 0

    zf = zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=1) if zip_path else None
    workers = max(1, min(4, int(os.cpu_count() or 1), len(records)))

    def prepare(record: dict[str, Any], name: str):
        bgra = _render(store, record, background=background, max_side=max_side)
        data = encode_bgra(
            bgra, fmt, quality=quality, background=background, dpi=dpi,
            png_optimize=png_optimize, png_quantize=png_quantize,
        )
        return bgra, data

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(prepare, record, name)
                       for record, name in zip(records, names)]
            for index, (record, name, future) in enumerate(zip(records, names, futures)):
                bgra, data = future.result()
                total_bytes += len(data)
                if zf is not None:
                    zf.writestr(name, data)
                else:
                    assert folder is not None
                    with open(folder / name, "wb") as fh:
                        fh.write(data)
                out_w, out_h = bgra.shape[1], bgra.shape[0]
                manifest.append({
                    "index": index + 1,
                    "output": name,
                    "source": _source_stem(record),
                    "width": out_w,
                    "height": out_h,
                    "rotation": int(record.get("rotation", 0) or 0),
                    "fine_angle": float(record.get("fine_angle", 0) or 0),
                    "manual": bool(record.get("manual")),
                })
                done += 1
                progress(done, total, f"正在导出 {index + 1}/{len(records)}")
        if options.get("include_manifest", True):
            payload = _manifest_bytes(manifest)
            total_bytes += len(payload)
            if zf is not None:
                zf.writestr("manifest.csv", payload)
            else:
                assert folder is not None
                with open(folder / "manifest.csv", "wb") as fh:
                    fh.write(payload)
            done += 1
            progress(done, total, "正在写入清单")
    finally:
        if zf is not None:
            zf.close()
    return manifest, total_bytes


def export_folder(store, records, options, progress: Progress) -> dict[str, Any]:
    final = unique_path(store.resolve_export_path(options.get("path"), "folder"), is_dir=True)
    temp = final.with_name(final.name + ".part")
    if temp.exists():
        shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True, exist_ok=False)
    try:
        manifest, total_bytes = _write_image_set(store, records, options, progress, folder=temp)
        os.replace(temp, final)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return {
        "kind": "folder",
        "path": str(final),
        "count": len(records),
        "size_bytes": total_bytes,
        "files": [str(final / row["output"]) for row in manifest],
        "manifest": [row for row in manifest],
    }


def export_zip(store, records, options, progress: Progress) -> dict[str, Any]:
    final = unique_path(store.resolve_export_path(options.get("path"), "zip"), is_dir=False)
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = final.with_name(final.name + ".part")
    try:
        manifest, total_bytes = _write_image_set(store, records, options, progress, zip_path=temp)
        os.replace(temp, final)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {
        "kind": "zip",
        "path": str(final),
        "count": len(records),
        "size_bytes": total_bytes,
        "files": [row["output"] for row in manifest],
    }


# ---------------------------------------------------------------------------
# PDF


PAGE_SIZES_MM = {
    "a4": (210.0, 297.0),
    "a3": (297.0, 420.0),
    "letter": (215.9, 279.4),
}


def _fit_size(width: int, height: int, max_w: int, max_h: int) -> tuple[int, int]:
    scale = min(max_w / float(width), max_h / float(height))
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def export_pdf(store, records, options, progress: Progress) -> dict[str, Any]:
    page_mode = str(_option(options, "page", "a4")).lower()
    if page_mode not in {"a4", "a3", "letter", "auto"}:
        page_mode = "a4"
    orientation = str(_option(options, "orientation", "auto")).lower()
    if orientation not in {"auto", "portrait", "landscape"}:
        orientation = "auto"
    dpi = _dpi(_option(options, "dpi", 200))
    quality = _quality(_option(options, "quality", 92))
    background = _background(options, "white") or (255, 255, 255)
    try:
        margin_mm = float(clamp(float(_option(options, "margin_mm", 0)), 0, 50))
    except (TypeError, ValueError):
        margin_mm = 0.0
    margin_px = int(round(margin_mm / 25.4 * dpi))

    pages: list[Image.Image] = []
    total = len(records) + 1
    for index, record in enumerate(records):
        bgra = _render(store, record, background=background)
        rgb = cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGB)
        image = Image.fromarray(rgb)

        if page_mode == "auto":
            page = image
        else:
            page_w_mm, page_h_mm = PAGE_SIZES_MM[page_mode]
            landscape = orientation == "landscape" or (
                orientation == "auto" and image.width > image.height
            )
            short, long = sorted((page_w_mm, page_h_mm))
            if landscape:
                page_w_mm, page_h_mm = long, short
            page_w = max(1, int(round(page_w_mm / 25.4 * dpi)))
            page_h = max(1, int(round(page_h_mm / 25.4 * dpi)))
            canvas = Image.new("RGB", (page_w, page_h), tuple(reversed(background)))
            inner_w = max(1, page_w - margin_px * 2)
            inner_h = max(1, page_h - margin_px * 2)
            new_w, new_h = _fit_size(image.width, image.height, inner_w, inner_h)
            resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
            x = (page_w - new_w) // 2
            y = (page_h - new_h) // 2
            canvas.paste(resized, (x, y))
            page = canvas
        pages.append(page)
        progress(index + 1, total, f"正在排版 {index + 1}/{len(records)}")

    if not pages:
        raise UserError("没有可导出的图片。")

    final = unique_path(store.resolve_export_path(options.get("path"), "pdf"), is_dir=False)
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = final.with_name(final.name + ".part")
    try:
        pages[0].save(
            temp,
            format="PDF",
            save_all=True,
            append_images=pages[1:],
            resolution=float(dpi),
            quality=quality,
            title=str(_option(options, "title", "PaperTrim export") or "PaperTrim export"),
            author="PaperTrim",
            creator="PaperTrim",
            subject="Scanned pages cropped by PaperTrim",
        )
        os.replace(temp, final)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    progress(total, total, "PDF 已生成")
    return {
        "kind": "pdf",
        "path": str(final),
        "count": len(records),
        "size_bytes": final.stat().st_size,
        "pages": len(records),
        "dpi": dpi,
    }


# ---------------------------------------------------------------------------
# Long image


def export_long(store, records, options, progress: Progress) -> dict[str, Any]:
    direction = str(_option(options, "direction", "vertical")).lower()
    if direction not in {"vertical", "horizontal"}:
        direction = "vertical"
    fmt = _format(_option(options, "format", "jpg"))
    quality = _quality(_option(options, "quality", 92))
    background = _background(options, "white") or (255, 255, 255)
    try:
        gap = int(clamp(float(_option(options, "gap", 24)), 0, 400))
    except (TypeError, ValueError):
        gap = 24
    try:
        target = int(clamp(float(_option(options, "target_size", 1600)), 200, 8000))
    except (TypeError, ValueError):
        target = 1600
    dpi = _dpi(_option(options, "dpi", 300))

    # First pass: exact aspect ratios at a small size (cheap), so the canvas can
    # be allocated before full-resolution renders.
    aspects: list[tuple[int, int]] = []
    for index, record in enumerate(records):
        small = _render(store, record, max_side=360)
        aspects.append((small.shape[1], small.shape[0]))
        progress(index + 1, len(records) * 2 + 1, f"正在测量 {index + 1}/{len(records)}")

    if direction == "vertical":
        widths = [target for _ in records]
        heights = [
            max(1, int(round(target * h / w))) if w else target
            for w, h in aspects
        ]
        total_w = target
        total_h = sum(heights) + gap * max(0, len(records) - 1)
        scale = min(1.0, 60000.0 / max(total_w, total_h))
        if scale < 1.0:
            target = max(200, int(target * scale))
            widths = [target for _ in records]
            heights = [max(1, int(round(target * h / w))) if w else target for w, h in aspects]
            total_w = target
            total_h = sum(heights) + gap * max(0, len(records) - 1)
        if max(total_w, total_h) > 60000:
            raise UserError("拼接后的长图过大，请减少图片数量或调小统一宽度。")
        canvas = np.zeros((total_h, total_w, 4), dtype=np.uint8)
        canvas[:, :, :3] = background
        canvas[:, :, 3] = 255
        y = 0
        for index, (record, height) in enumerate(zip(records, heights)):
            bgra = _render(store, record, background=background)
            if bgra.shape[1] != target:
                bgra = cv2.resize(bgra, (target, max(1, height)), interpolation=cv2.INTER_AREA)
            canvas[y:y + bgra.shape[0], 0:target, :] = bgra
            y += bgra.shape[0] + gap
            progress(len(records) + index + 1, len(records) * 2 + 1,
                     f"正在拼接 {index + 1}/{len(records)}")
        result = canvas
    else:
        heights_target = [target for _ in records]
        widths = [
            max(1, int(round(target * w / h))) if h else target
            for w, h in aspects
        ]
        total_w = sum(widths) + gap * max(0, len(records) - 1)
        total_h = target
        scale = min(1.0, 60000.0 / max(total_w, total_h))
        if scale < 1.0:
            target = max(200, int(target * scale))
            heights_target = [target for _ in records]
            widths = [max(1, int(round(target * w / h))) if h else target for w, h in aspects]
            total_w = sum(widths) + gap * max(0, len(records) - 1)
            total_h = target
        if max(total_w, total_h) > 60000:
            raise UserError("拼接后的长图过大，请减少图片数量或调小统一尺寸。")
        canvas = np.zeros((total_h, total_w, 4), dtype=np.uint8)
        canvas[:, :, :3] = background
        canvas[:, :, 3] = 255
        x = 0
        for index, (record, width) in enumerate(zip(records, widths)):
            bgra = _render(store, record, background=background)
            if bgra.shape[0] != target:
                bgra = cv2.resize(bgra, (max(1, width), target), interpolation=cv2.INTER_AREA)
            canvas[0:target, x:x + bgra.shape[1], :] = bgra
            x += bgra.shape[1] + gap
            progress(len(records) + index + 1, len(records) * 2 + 1,
                     f"正在拼接 {index + 1}/{len(records)}")
        result = canvas

    data = encode_bgra(result, fmt, quality=quality, background=background, dpi=dpi)
    final = store.resolve_export_path(options.get("path"), "long")
    if fmt != "jpg":
        final = final.with_suffix("." + fmt)
    final = unique_path(final, is_dir=False)
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = final.with_name(final.name + ".part")
    try:
        with open(temp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, final)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    progress(len(records) * 2 + 1, len(records) * 2 + 1, "长图已生成")
    return {
        "kind": "long",
        "path": str(final),
        "count": len(records),
        "size_bytes": final.stat().st_size,
        "width": int(result.shape[1]),
        "height": int(result.shape[0]),
        "direction": direction,
    }


# ---------------------------------------------------------------------------
# Entry point


def run_export(store, records: list[dict[str, Any]], kind: str,
               options: dict[str, Any], progress: Progress | None = None) -> dict[str, Any]:
    if not records:
        raise UserError("没有可导出的图片。")
    bad = [r for r in records if r.get("status") != "ready" or not r.get("polygon")]
    if bad:
        raise UserError("有图片尚未识别完成，请先处理后再导出。")
    progress = progress or _noop
    options = options or {}
    if kind == "folder":
        return export_folder(store, records, options, progress)
    if kind == "pdf":
        return export_pdf(store, records, options, progress)
    if kind == "long":
        return export_long(store, records, options, progress)
    if kind == "zip":
        return export_zip(store, records, options, progress)
    raise UserError("未知的导出类型。")
