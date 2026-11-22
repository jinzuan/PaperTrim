"""Image loading, polygon masks, rotation and encoding.

The paper is described by a polygon in *source image* coordinates.  A render
turns that polygon into an anti-aliased alpha mask, rotates the cut-out, crops
to its visible bounds and optionally composites it onto a colour.
"""

from __future__ import annotations

import io
import math
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
from PIL import Image, ImageOps

from .util import UserError, clamp


# ---------------------------------------------------------------------------
# Loading


def _pil_to_bgr(im: Image.Image) -> np.ndarray:
    rgb = np.asarray(im.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def load_bgr(path: str | Path) -> np.ndarray:
    """Load the first frame, honour EXIF orientation and return BGR uint8."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        return _pil_to_bgr(im)


def read_image_size(path: str | Path) -> tuple[int, int]:
    """Return EXIF-oriented (width, height)."""
    with Image.open(path) as im:
        try:
            orientation = int(im.getexif().get(274, 1))
        except Exception:
            orientation = 1
        width, height = im.size
    if orientation in (5, 6, 7, 8):
        width, height = height, width
    return int(width), int(height)


class ImageCache:
    """A tiny thread-safe LRU for decoded source images."""

    def __init__(self, max_items: int = 3):
        self.max_items = max(1, int(max_items))
        self._items: OrderedDict[tuple[str, int, int], np.ndarray] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, path: str | Path) -> np.ndarray:
        path = os.fspath(path)
        try:
            st = os.stat(path)
        except OSError as exc:
            raise UserError(f"找不到图片文件：{os.path.basename(path)}") from exc
        key = (path, int(st.st_mtime_ns), int(st.st_size))
        with self._lock:
            hit = self._items.get(key)
            if hit is not None:
                self._items.move_to_end(key)
                return hit
        image = load_bgr(path)
        with self._lock:
            self._items[key] = image
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
        return image


# ---------------------------------------------------------------------------
# Masks


def _clamp_polygon(polygon: Iterable[Iterable[float]], width: int, height: int) -> np.ndarray:
    points = []
    for point in polygon:
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        points.append([clamp(x, 0.0, float(width - 1)), clamp(y, 0.0, float(height - 1))])
    if len(points) < 3:
        raise UserError("裁切轮廓至少需要 3 个点。")
    return np.asarray(points, dtype=np.float32)


def build_mask(
    shape: tuple[int, int],
    polygon: Iterable[Iterable[float]],
    strokes: Iterable[dict[str, Any]] | None = None,
    expand: float = 0,
    feather: float = 0.8,
    holes: Iterable[Iterable[Iterable[float]]] | None = None,
    curve: dict[str, Any] | None = None,
) -> np.ndarray:
    """Rasterise polygon + holes + brush corrections into alpha."""
    height, width = int(shape[0]), int(shape[1])
    points = _clamp_polygon(polygon, width, height)
    mask = np.zeros((height, width), dtype=np.uint8)
    dense = _smooth_points(points, curve)
    cv2.fillPoly(mask, [np.rint(dense).astype(np.int32)], 255, lineType=cv2.LINE_AA)

    for hole in holes or []:
        try:
            hole_points = _clamp_polygon(hole, width, height)
        except UserError:
            continue
        hole_dense = _smooth_points(hole_points, curve)
        cv2.fillPoly(mask, [np.rint(hole_dense).astype(np.int32)], 0, lineType=cv2.LINE_AA)

    for stroke in strokes or []:
        try:
            raw = stroke.get("points") or []
            mode = str(stroke.get("mode", "add"))
            radius = int(clamp(float(stroke.get("radius", 24)), 1, 500))
            pts = np.asarray([[float(p[0]), float(p[1])] for p in raw], dtype=np.float32)
        except (TypeError, ValueError, IndexError):
            continue
        if len(pts) == 0:
            continue
        pts[:, 0] = np.clip(pts[:, 0], 0, width - 1)
        pts[:, 1] = np.clip(pts[:, 1], 0, height - 1)
        value = 255 if mode == "add" else 0
        ipts = np.rint(pts).astype(np.int32)
        if len(ipts) == 1:
            cv2.circle(mask, tuple(ipts[0]), radius, value, -1, lineType=cv2.LINE_AA)
        else:
            cv2.polylines(mask, [ipts], False, value, thickness=max(1, radius * 2), lineType=cv2.LINE_AA)
            # Round caps look better than the square-ish ends of polylines.
            cv2.circle(mask, tuple(ipts[0]), radius, value, -1, lineType=cv2.LINE_AA)
            cv2.circle(mask, tuple(ipts[-1]), radius, value, -1, lineType=cv2.LINE_AA)

    expand = int(round(clamp(float(expand or 0), -40, 40)))
    if expand > 0:
        k = expand * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        mask = cv2.dilate(mask, kernel)
    elif expand < 0:
        k = abs(expand) * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        mask = cv2.erode(mask, kernel)

    feather = float(clamp(float(feather or 0), 0.0, 8.0))
    if feather > 0:
        mask = cv2.GaussianBlur(mask, (0, 0), feather)
    return mask


# ---------------------------------------------------------------------------
# Rotation / rendering


def _rotate_90(bgra: np.ndarray, degrees: int) -> np.ndarray:
    degrees = int(degrees) % 360
    if degrees == 0:
        return bgra
    if degrees == 90:      # clockwise
        return np.ascontiguousarray(np.rot90(bgra, k=-1))
    if degrees == 180:
        return np.ascontiguousarray(np.rot90(bgra, k=2))
    if degrees == 270:
        return np.ascontiguousarray(np.rot90(bgra, k=1))
    return bgra


def rotate_fine(bgra: np.ndarray, clockwise_degrees: float) -> np.ndarray:
    """Rotate around the centre, expanding the canvas so nothing is lost."""
    angle = float(clockwise_degrees or 0)
    if abs(angle) < 1e-6:
        return bgra
    height, width = bgra.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), -angle, 1.0)
    cos = abs(matrix[0, 0])
    sin = abs(matrix[0, 1])
    new_w = max(1, int(round(height * sin + width * cos)))
    new_h = max(1, int(round(height * cos + width * sin)))
    matrix[0, 2] += new_w / 2.0 - width / 2.0
    matrix[1, 2] += new_h / 2.0 - height / 2.0
    return cv2.warpAffine(
        bgra, matrix, (new_w, new_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )


def parse_background(value: Any) -> tuple[int, int, int] | None:
    """Return a BGR colour, or None for transparency."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (int(value[0]), int(value[1]), int(value[2]))
    text = str(value).strip().lower()
    if text in {"", "transparent", "none", "alpha"}:
        return None
    named = {
        "white": (255, 255, 255),
        "black": (0, 0, 0),
        "gray": (128, 128, 128),
        "grey": (128, 128, 128),
        "red": (60, 60, 200),
        "blue": (200, 120, 40),
        "green": (80, 160, 80),
    }
    if text in named:
        return named[text]
    if text.startswith("#") and len(text) == 7:
        try:
            r = int(text[1:3], 16)
            g = int(text[3:5], 16)
            b = int(text[5:7], 16)
            return (b, g, r)
        except ValueError:
            raise UserError(f"无法识别的颜色：{value}")
    raise UserError(f"无法识别的颜色：{value}")


DEFAULT_OUTPUT: dict[str, Any] = {
    "mode": "cutout",              # cutout | rect
    "rectification": "auto",       # auto | rotate | quad
    "fill": "paper",               # paper | color | white | transparent | original
    "color": "#ffffff",
    "padding": 0,                  # px around the result
    "aspect": "paper",             # paper | custom
    "aspect_w": 210.0,
    "aspect_h": 297.0,
    "edge": "fit",                 # fit = reconstructed original sides; actual = torn contour
    "edge_margin": 0,              # px, positive keeps more surrounding paper
    "enhance": False,              # paper whitening / colour purity
    "whiten": 35,                  # 0..100
    "purity": 35,                  # 0..100
    "desaturate": 0,               # 0..100, 0 = original colour, 100 = black & white
    "mono_boost": False,           # extra contrast when desaturating
}

DEFAULT_CURVE: dict[str, Any] = {
    "enabled": False,
    "amount": 0.55,
}


def normalize_curve(raw: dict[str, Any] | None, base: dict[str, Any] | None = None) -> dict[str, Any]:
    curve = dict(DEFAULT_CURVE)
    if base:
        curve.update({key: value for key, value in base.items() if key in DEFAULT_CURVE})
    if raw:
        curve.update({key: value for key, value in raw.items() if key in DEFAULT_CURVE})
    curve["enabled"] = bool(curve.get("enabled", False))
    curve["amount"] = float(clamp(float(curve.get("amount", 0.55) or 0.55), 0.0, 1.0))
    return curve


def _smooth_points(points: np.ndarray, curve: dict[str, Any] | None) -> np.ndarray:
    curve = normalize_curve(curve)
    if not curve["enabled"] or len(points) < 3:
        return points
    amount = curve["amount"]
    if amount <= 0.01:
        return points
    points = np.asarray(points, dtype=np.float32)
    count = len(points)
    dense: list[list[float]] = []
    steps = 6
    for index in range(count):
        p0 = points[(index - 1) % count]
        p1 = points[index]
        p2 = points[(index + 1) % count]
        p3 = points[(index + 2) % count]
        for step in range(steps):
            t = step / float(steps)
            t2 = t * t
            t3 = t2 * t
            catmull = 0.5 * (
                2.0 * p1
                + (-p0 + p2) * t
                + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
                + (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
            )
            linear = p1 + (p2 - p1) * t
            point = linear + (catmull - linear) * amount
            dense.append([float(point[0]), float(point[1])])
    return np.asarray(dense, dtype=np.float32)



def normalize_output(raw: dict[str, Any] | None, base: dict[str, Any] | None = None) -> dict[str, Any]:
    output = dict(DEFAULT_OUTPUT)
    if base:
        output.update({key: value for key, value in base.items() if key in DEFAULT_OUTPUT})
    if raw:
        output.update({key: value for key, value in raw.items() if key in DEFAULT_OUTPUT})
    if output["mode"] not in {"cutout", "rect"}:
        output["mode"] = "cutout"
    if output["rectification"] not in {"auto", "rotate", "quad"}:
        output["rectification"] = "auto"
    if output["fill"] not in {"paper", "color", "white", "transparent", "original"}:
        output["fill"] = "paper"
    if output["aspect"] not in {"paper", "custom"}:
        output["aspect"] = "paper"
    try:
        parse_background(output.get("color") or "#ffffff")
    except UserError:
        output["color"] = "#ffffff"
    output["color"] = str(output["color"]).lower()
    output["padding"] = int(clamp(float(output.get("padding", 0) or 0), 0, 1000))
    output["aspect_w"] = float(clamp(float(output.get("aspect_w", 210) or 210), 0.1, 10000))
    output["aspect_h"] = float(clamp(float(output.get("aspect_h", 297) or 297), 0.1, 10000))
    if output.get("edge") not in {"fit", "actual"}:
        output["edge"] = "fit"
    output["edge_margin"] = int(clamp(float(output.get("edge_margin", 0) or 0), -60, 60))
    value = output.get("enhance")
    output["enhance"] = value is True or (isinstance(value, str) and value.lower() in {"1", "true", "on", "yes"})
    output["whiten"] = int(clamp(float(output.get("whiten", 35) or 0), 0, 100))
    output["purity"] = int(clamp(float(output.get("purity", 35) or 0), 0, 100))
    output["desaturate"] = int(clamp(float(output.get("desaturate", 0) or 0), 0, 100))
    boost = output.get("mono_boost")
    output["mono_boost"] = boost is True or (isinstance(boost, str) and boost.lower() in {"1", "true", "on", "yes"})
    return output


def _colour(value: Any, fallback: str = "#ffffff") -> tuple[int, int, int]:
    try:
        parsed = parse_background(value)
    except UserError:
        parsed = None
    if parsed is None:
        parsed = parse_background(fallback) or (255, 255, 255)
    return parsed


def _quad_points(record: dict[str, Any]) -> np.ndarray | None:
    raw = record.get("quad") or []
    if len(raw) == 4:
        try:
            quad = np.asarray([[float(p[0]), float(p[1])] for p in raw], dtype=np.float32)
        except (TypeError, ValueError, IndexError):
            quad = None
        if quad is not None and np.isfinite(quad).all():
            return quad
    polygon = record.get("polygon") or []
    if len(polygon) < 3:
        return None
    points = np.asarray(polygon, dtype=np.float32).reshape(-1, 1, 2)
    _center, _size, _angle = cv2.minAreaRect(points)
    return cv2.boxPoints((_center, _size, _angle)).astype(np.float32)


def _order_quad(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    total = points.sum(axis=1)
    diff = points[:, 0] - points[:, 1]
    ordered = np.array([
        points[int(np.argmin(total))],
        points[int(np.argmax(diff))],
        points[int(np.argmax(total))],
        points[int(np.argmin(diff))],
    ], dtype=np.float32)
    if len(np.unique(ordered, axis=0)) < 4:
        center = points.mean(axis=0)
        angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
        points = points[np.argsort(angles)]
        start = int(np.argmin(points.sum(axis=1)))
        ordered = np.roll(points, -start, axis=0)
    return ordered


def _rotate_mask(mask: np.ndarray, degrees: int, fine: float) -> np.ndarray:
    bgra = cv2.merge([mask, mask, mask, mask])
    bgra = _rotate_90(bgra, degrees)
    if abs(fine) > 1e-6:
        bgra = rotate_fine(bgra, fine)
    return bgra[:, :, 3]


def _add_padding(bgra: np.ndarray, padding: int,
                 fill: tuple[int, int, int] | None) -> np.ndarray:
    padding = int(clamp(padding, 0, 1000))
    if padding <= 0:
        return bgra
    height, width = bgra.shape[:2]
    canvas = np.zeros((height + padding * 2, width + padding * 2, 4), dtype=np.uint8)
    if fill is not None:
        canvas[:, :, :3] = fill
        canvas[:, :, 3] = 255
    canvas[padding:padding + height, padding:padding + width] = bgra
    return canvas


def _resize_to_ratio(bgra: np.ndarray, ratio: float) -> np.ndarray:
    height, width = bgra.shape[:2]
    area = max(1.0, float(width) * float(height))
    ratio = float(clamp(ratio, 0.01, 100.0))
    out_w = max(1, int(round(math.sqrt(area * ratio))))
    out_h = max(1, int(round(out_w / ratio)))
    if out_w == width and out_h == height:
        return bgra
    return cv2.resize(bgra, (out_w, out_h), interpolation=cv2.INTER_AREA)


def _quad_side_lengths(quad: np.ndarray) -> tuple[float, float, float, float]:
    top = float(np.linalg.norm(quad[1] - quad[0]))
    right = float(np.linalg.norm(quad[2] - quad[1]))
    bottom = float(np.linalg.norm(quad[3] - quad[2]))
    left = float(np.linalg.norm(quad[0] - quad[3]))
    return top, right, bottom, left


def _resolve_mode(record: dict[str, Any], output: dict[str, Any]) -> str:
    if output["mode"] != "rect":
        return "cutout"
    if output["rectification"] == "rotate":
        return "rect-rotate"
    if output["rectification"] == "quad":
        return "rect-quad"
    if record.get("quad_manual") and len(record.get("quad") or []) == 4:
        return "rect-quad"
    detection = record.get("detection") or {}
    if detection.get("quad_near_rect") is True:
        return "rect-rotate"
    if record.get("quad") and len(record.get("quad") or []) == 4:
        return "rect-quad"
    return "rect-rotate"


def _lighten_colour(bgr: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    amount = float(clamp(amount, 0.0, 1.0))
    return tuple(int(round(channel + (255 - channel) * amount)) for channel in bgr)


def enhance_paper(bgr: np.ndarray, record: dict[str, Any], output: dict[str, Any]) -> np.ndarray:
    """Whiten the paper while keeping ink colours saturated.

    This is deliberately a gentle levels/white-balance pass.  It is disabled
    unless output["enhance"] is true, so default rendering is untouched.
    """
    if not output.get("enhance"):
        return bgr
    whiten = float(clamp(float(output.get("whiten", 35) or 0), 0, 100)) / 100.0
    purity = float(clamp(float(output.get("purity", 35) or 0), 0, 100)) / 100.0
    if whiten <= 0 and purity <= 0:
        return bgr
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    lightness, a_channel, b_channel = cv2.split(lab)
    paper_bgr = np.uint8([[_colour(record.get("paper_color"), "#ffffff")]])
    paper_lab = cv2.cvtColor(paper_bgr, cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)
    paper_l = float(paper_lab[0])
    if paper_l < 20:
        paper_l = float(np.percentile(lightness, 90))
    black = float(np.percentile(lightness, 2))
    denominator = max(24.0, paper_l - black)
    stretched = np.clip((lightness - black) / denominator * 255.0, 0, 255)
    if whiten > 0:
        lightness = lightness * (1.0 - whiten) + stretched * whiten
        a_channel = a_channel - ((paper_lab[1] - 128.0) * 0.8 * whiten)
        b_channel = b_channel - ((paper_lab[2] - 128.0) * 0.8 * whiten)
    if purity > 0:
        a_channel = 128.0 + (a_channel - 128.0) * (1.0 + purity * 1.4)
        b_channel = 128.0 + (b_channel - 128.0) * (1.0 + purity * 1.4)
    result = cv2.merge([
        np.clip(lightness, 0, 255),
        np.clip(a_channel, 0, 255),
        np.clip(b_channel, 0, 255),
    ]).astype(np.uint8)
    return cv2.cvtColor(result, cv2.COLOR_LAB2BGR)


def apply_desaturate(bgr: np.ndarray, amount: float = 100, boost: bool = False) -> np.ndarray:
    """Blend the image toward grayscale; 100 is full black & white.

    The optional boost works on the grayscale layer (levels + local contrast)
    before blending, so partial desaturation still keeps some original colour.
    """
    amount = float(clamp(amount, 0.0, 100.0)) / 100.0
    if amount <= 0:
        return bgr
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if boost:
        low, high = np.percentile(gray, (1.5, 98.5))
        if high - low > 8:
            gray = np.clip((gray.astype(np.float32) - low) / (high - low) * 255.0, 0, 255).astype(np.uint8)
        clahe = cv2.createCLAHE(clipLimit=1.7, tileGridSize=(8, 8))
        gray = clahe.apply(gray)
        blur = cv2.GaussianBlur(gray, (0, 0), 1.1)
        gray = cv2.addWeighted(gray, 1.35, blur, -0.35, 0)
    gray_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if amount >= 0.999:
        return gray_bgr
    return cv2.addWeighted(bgr, 1.0 - amount, gray_bgr, amount, 0)


def _desaturate_colour(colour: tuple[int, int, int] | None,
                       amount: float) -> tuple[int, int, int] | None:
    if colour is None:
        return None
    amount = float(clamp(amount, 0.0, 100.0)) / 100.0
    if amount <= 0:
        return colour
    gray = 0.114 * colour[0] + 0.587 * colour[1] + 0.299 * colour[2]
    return tuple(int(round(channel * (1.0 - amount) + gray * amount)) for channel in colour)


def _fill_colour(record: dict[str, Any], output: dict[str, Any],
                 for_padding: bool = False) -> tuple[int, int, int] | None:
    fill = output.get("fill", "paper")
    if fill == "transparent":
        return None
    amount = float(output.get("desaturate", 0) or 0)
    if fill == "paper":
        colour = _colour(record.get("paper_color") or "#ffffff")
        if output.get("enhance"):
            colour = _lighten_colour(colour, float(output.get("whiten", 0) or 0) / 100.0)
        return _desaturate_colour(colour, amount)
    if fill == "white":
        return (255, 255, 255)
    if fill == "color":
        return _desaturate_colour(_colour(output.get("color") or "#ffffff"), amount)
    if fill == "original":
        if not for_padding:
            return None
        return _desaturate_colour(_colour(output.get("color") or "#ffffff"), amount)
    return (255, 255, 255)


def _render_cutout_mode(source_bgr: np.ndarray, mask: np.ndarray,
                        record: dict[str, Any], output: dict[str, Any]) -> np.ndarray:
    bgra = cv2.merge([
        source_bgr[:, :, 0], source_bgr[:, :, 1], source_bgr[:, :, 2], mask
    ])
    bgra = _rotate_90(bgra, int(record.get("rotation", 0) or 0))
    fine = float(record.get("fine_angle", 0) or 0)
    if abs(fine) > 1e-6:
        bgra = rotate_fine(bgra, fine)
    ys, xs = np.where(bgra[:, :, 3] > 0)
    if len(xs) == 0:
        raise UserError("裁切结果为空，请检查轮廓。")
    x0, x1 = max(0, int(xs.min()) - 1), min(bgra.shape[1] - 1, int(xs.max()) + 1)
    y0, y1 = max(0, int(ys.min()) - 1), min(bgra.shape[0] - 1, int(ys.max()) + 1)
    bgra = np.ascontiguousarray(bgra[y0:y1 + 1, x0:x1 + 1])
    return _add_padding(bgra, int(output.get("padding", 0) or 0), None)


def _paper_rect_points(record: dict[str, Any]) -> np.ndarray | None:
    points = record.get("paper_rect") or []
    if len(points) != 4:
        return None
    return np.asarray(points, dtype=np.float32).reshape(4, 2)


def _rotate_points_90(points: np.ndarray, degrees: int, width: int, height: int) -> np.ndarray:
    degrees = int(degrees) % 360
    pts = np.asarray(points, dtype=np.float32).copy()
    if degrees == 90:       # clockwise
        pts[:, 0], pts[:, 1] = height - 1 - pts[:, 1], pts[:, 0]
    elif degrees == 180:
        pts[:, 0], pts[:, 1] = width - 1 - pts[:, 0], height - 1 - pts[:, 1]
    elif degrees == 270:
        pts[:, 0], pts[:, 1] = pts[:, 1], width - 1 - pts[:, 0]
    return pts


def _rotate_points_fine(points: np.ndarray, degrees: float, width: int, height: int
                        ) -> tuple[np.ndarray, int, int]:
    if abs(degrees) < 1e-6:
        return points, width, height
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), -degrees, 1.0)
    cos = abs(matrix[0, 0])
    sin = abs(matrix[0, 1])
    new_w = max(1, int(round(height * sin + width * cos)))
    new_h = max(1, int(round(height * cos + width * sin)))
    matrix[0, 2] += new_w / 2.0 - width / 2.0
    matrix[1, 2] += new_h / 2.0 - height / 2.0
    rotated = points @ matrix[:, :2].T + matrix[:, 2]
    return rotated.astype(np.float32), new_w, new_h


def _paper_rect_bbox(record: dict[str, Any], source_shape: tuple[int, int],
                     output: dict[str, Any] | None = None) -> tuple[int, int, int, int] | None:
    if output is None:
        output = normalize_output(record.get("output"))
    if output.get("edge") == "actual":
        return None
    rect = _paper_rect_points(record)
    if rect is None:
        return None
    height, width = int(source_shape[0]), int(source_shape[1])
    rotation = int(record.get("rotation", 0) or 0) % 360
    rect = _rotate_points_90(rect, rotation, width, height)
    if rotation in (90, 270):
        width, height = height, width
    fine = float(record.get("fine_angle", 0) or 0)
    rect, width, height = _rotate_points_fine(rect, fine, width, height)
    margin = int(output.get("edge_margin", 0) or 0)
    x0 = max(0, int(np.floor(rect[:, 0].min())) - 1 - margin)
    x1 = min(width - 1, int(np.ceil(rect[:, 0].max())) + 1 + margin)
    y0 = max(0, int(np.floor(rect[:, 1].min())) - 1 - margin)
    y1 = min(height - 1, int(np.ceil(rect[:, 1].max())) + 1 + margin)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, x1, y0, y1


def _render_rect_rotate(source_bgr: np.ndarray, mask: np.ndarray,
                        record: dict[str, Any], output: dict[str, Any]) -> np.ndarray:
    rotation = int(record.get("rotation", 0) or 0)
    fine = float(record.get("fine_angle", 0) or 0)

    cutout = cv2.merge([
        source_bgr[:, :, 0], source_bgr[:, :, 1], source_bgr[:, :, 2], mask
    ])
    cutout = _rotate_90(cutout, rotation)
    if abs(fine) > 1e-6:
        cutout = rotate_fine(cutout, fine)

    bbox = _paper_rect_bbox(record, source_bgr.shape[:2], output)
    if bbox is None:
        alpha = cutout[:, :, 3]
        ys, xs = np.where(alpha > 0)
        if len(xs) == 0:
            raise UserError("裁切结果为空，请检查轮廓。")
        x0, x1 = max(0, int(xs.min()) - 1), min(cutout.shape[1] - 1, int(xs.max()) + 1)
        y0, y1 = max(0, int(ys.min()) - 1), min(cutout.shape[0] - 1, int(ys.max()) + 1)
    else:
        x0, x1, y0, y1 = bbox
    cutout = np.ascontiguousarray(cutout[y0:y1 + 1, x0:x1 + 1])

    fill = output.get("fill", "paper")
    if fill == "original":
        source = cv2.merge([
            source_bgr[:, :, 0], source_bgr[:, :, 1], source_bgr[:, :, 2],
            np.full((source_bgr.shape[0], source_bgr.shape[1]), 255, dtype=np.uint8),
        ])
        source = _rotate_90(source, rotation)
        if abs(fine) > 1e-6:
            source = rotate_fine(source, fine)
        source = np.ascontiguousarray(source[y0:y1 + 1, x0:x1 + 1])
        bgra = source
    else:
        fill_bgr = _fill_colour(record, output)
        bgra = composite_bgra(cutout, fill_bgr) if fill_bgr is not None else cutout

    if output.get("aspect") == "custom":
        ratio = float(output.get("aspect_w", 210)) / max(0.1, float(output.get("aspect_h", 297)))
        bgra = _resize_to_ratio(bgra, ratio)

    padding_fill = _fill_colour(record, output, for_padding=True)
    if fill == "transparent":
        padding_fill = None
    return _add_padding(bgra, int(output.get("padding", 0) or 0), padding_fill)


def _render_rect_quad(source_bgr: np.ndarray, mask: np.ndarray,
                      record: dict[str, Any], output: dict[str, Any]) -> np.ndarray:
    quad = _quad_points(record)
    if quad is None:
        return _render_rect_rotate(source_bgr, mask, record, output)
    quad = _order_quad(quad)
    top, right, bottom, left = _quad_side_lengths(quad)
    avg_w = max(1.0, (top + bottom) / 2.0)
    avg_h = max(1.0, (left + right) / 2.0)
    content_w = max(1, int(round(avg_w)))
    content_h = max(1, int(round(avg_h)))

    destination = np.array([
        [0, 0], [content_w - 1, 0], [content_w - 1, content_h - 1], [0, content_h - 1],
    ], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(quad, destination)
    warped_source = cv2.warpPerspective(
        source_bgr, matrix, (content_w, content_h),
        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101,
    )
    warped_mask = cv2.warpPerspective(
        mask, matrix, (content_w, content_h),
        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
    )
    fill = output.get("fill", "paper")
    if fill == "original":
        bgra = cv2.merge([
            warped_source[:, :, 0], warped_source[:, :, 1], warped_source[:, :, 2],
            np.full((content_h, content_w), 255, dtype=np.uint8),
        ])
    else:
        cutout = cv2.merge([
            warped_source[:, :, 0], warped_source[:, :, 1], warped_source[:, :, 2],
            warped_mask,
        ])
        fill_bgr = _fill_colour(record, output)
        bgra = composite_bgra(cutout, fill_bgr) if fill_bgr is not None else cutout

    rotation = int(record.get("rotation", 0) or 0)
    bgra = _rotate_90(bgra, rotation)

    if output.get("aspect") == "custom":
        ratio = float(output.get("aspect_w", 210)) / max(0.1, float(output.get("aspect_h", 297)))
        bgra = _resize_to_ratio(bgra, ratio)

    padding_fill = _fill_colour(record, output, for_padding=True)
    if fill == "transparent":
        padding_fill = None
    return _add_padding(bgra, int(output.get("padding", 0) or 0), padding_fill)


def render_editor_preview(
    source_bgr: np.ndarray,
    record: dict[str, Any],
    max_side: int = 1800,
) -> np.ndarray:
    """Render the output with the editable outline/handles baked in.

    Used only by the editor's source/result toggle; exports never include it.
    """
    overlay = source_bgr.copy()
    height, width = overlay.shape[:2]
    thickness = max(2, int(round(min(width, height) * 0.0018)))
    polygon = record.get("polygon") or []
    if len(polygon) >= 3:
        points = np.asarray(polygon, dtype=np.int32).reshape(-1, 1, 2)
        cv2.polylines(overlay, [points], True, (40, 160, 255), thickness, cv2.LINE_AA)
        radius = max(3, int(round(min(width, height) * 0.004)))
        for x, y in polygon:
            center = (int(round(x)), int(round(y)))
            cv2.circle(overlay, center, radius, (255, 255, 255), -1, cv2.LINE_AA)
            cv2.circle(overlay, center, radius, (40, 160, 255), max(1, thickness // 2), cv2.LINE_AA)
    quad = record.get("quad") or []
    if len(quad) == 4:
        points = np.asarray(quad, dtype=np.int32).reshape(-1, 1, 2)
        cv2.polylines(overlay, [points], True, (80, 220, 120), max(2, thickness), cv2.LINE_AA)
    return render_record(overlay, record, max_side=max_side)


def render_record(
    source_bgr: np.ndarray,
    record: dict[str, Any],
    max_side: int | None = None,
    background: tuple[int, int, int] | None = None,
) -> np.ndarray:
    """Render according to the record's cutout/rect output settings."""
    height, width = source_bgr.shape[:2]
    output = normalize_output(record.get("output"))
    if output.get("enhance"):
        source_bgr = enhance_paper(source_bgr, record, output)
    if output.get("desaturate", 0) > 0:
        source_bgr = apply_desaturate(
            source_bgr, float(output.get("desaturate", 0)), bool(output.get("mono_boost")),
        )
    params = record.get("params") or {}
    mask = build_mask(
        (height, width),
        record.get("polygon") or [],
        record.get("strokes") or [],
        expand=params.get("expand", 0),
        feather=params.get("feather", 0.8),
        holes=record.get("holes") or [],
        curve=record.get("curve"),
    )
    mode = _resolve_mode(record, output)
    if mode == "cutout":
        bgra = _render_cutout_mode(source_bgr, mask, record, output)
    elif mode == "rect-quad":
        bgra = _render_rect_quad(source_bgr, mask, record, output)
    else:
        bgra = _render_rect_rotate(source_bgr, mask, record, output)

    if max_side:
        max_side = int(max_side)
        longest = max(bgra.shape[:2])
        if longest > max_side:
            scale = max_side / float(longest)
            bgra = cv2.resize(
                bgra,
                (max(1, int(round(bgra.shape[1] * scale))),
                 max(1, int(round(bgra.shape[0] * scale)))),
                interpolation=cv2.INTER_AREA,
            )
    if background is not None:
        bgra = composite_bgra(bgra, background)
    return bgra


def render_cutout(
    source_bgr: np.ndarray,
    record: dict[str, Any],
    max_side: int | None = None,
    background: tuple[int, int, int] | None = None,
) -> np.ndarray:
    """Backward-compatible name; delegates to the output-mode renderer."""
    return render_record(source_bgr, record, max_side=max_side, background=background)



def composite_bgra(bgra: np.ndarray, background: tuple[int, int, int]) -> np.ndarray:
    if bgra.shape[2] == 3:
        return bgra
    alpha = (bgra[:, :, 3:4].astype(np.float32) / 255.0)
    bg = np.zeros((1, 1, 3), dtype=np.float32)
    bg[0, 0] = background
    out = bgra[:, :, :3].astype(np.float32) * alpha + bg * (1.0 - alpha)
    result = np.empty_like(bgra)
    result[:, :, :3] = np.clip(out, 0, 255).astype(np.uint8)
    result[:, :, 3] = 255
    return result


def result_dimensions(record: dict[str, Any]) -> tuple[int, int]:
    """Approximate output size for the UI."""
    polygon = record.get("polygon") or []
    if not polygon:
        return (0, 0)
    output = normalize_output(record.get("output"))
    mode = _resolve_mode(record, output)
    padding = int(output.get("padding", 0) or 0) * 2

    paper_bbox = None
    if mode == "rect-rotate":
        paper_bbox = _paper_rect_bbox(
            record, (int(record.get("height") or 0), int(record.get("width") or 0)), output,
        )
    if mode == "rect-quad":
        quad = _quad_points(record)
        if quad is not None:
            top, right, bottom, left = _quad_side_lengths(_order_quad(quad))
            width = max(1.0, (top + bottom) / 2.0)
            height = max(1.0, (left + right) / 2.0)
        else:
            width, height = _polygon_size(polygon, record)
    elif mode == "rect-rotate":
        if paper_bbox is not None:
            width = float(paper_bbox[1] - paper_bbox[0] + 1)
            height = float(paper_bbox[3] - paper_bbox[2] + 1)
        else:
            width, height = _polygon_size(polygon, record)
    else:
        width, height = _polygon_size(polygon, record)

    if int(record.get("rotation", 0) or 0) % 180 == 90 and paper_bbox is None:
        width, height = height, width

    if output.get("aspect") == "custom":
        ratio = float(output.get("aspect_w", 210)) / max(0.1, float(output.get("aspect_h", 297)))
        area = width * height
        width = math.sqrt(max(1.0, area) * ratio)
        height = width / max(0.01, ratio)

    return (max(1, int(round(width + padding))), max(1, int(round(height + padding))))


def _polygon_size(polygon: list[list[float]], record: dict[str, Any]) -> tuple[float, float]:
    xs = [float(p[0]) for p in polygon]
    ys = [float(p[1]) for p in polygon]
    params = record.get("params") or {}
    expand = float(params.get("expand", 0) or 0)
    width = max(1.0, max(xs) - min(xs) + 2 * expand)
    height = max(1.0, max(ys) - min(ys) + 2 * expand)
    fine = abs(float(record.get("fine_angle", 0) or 0))
    if fine > 1e-6 and int(record.get("rotation", 0) or 0) % 180 != 90:
        radians = math.radians(fine)
        cos = abs(math.cos(radians))
        sin = abs(math.sin(radians))
        width, height = width * cos + height * sin, width * sin + height * cos
    return width, height


# ---------------------------------------------------------------------------
# Encoding


def encode_bgra(
    bgra: np.ndarray,
    fmt: str = "png",
    quality: int = 92,
    background: tuple[int, int, int] | None = None,
    dpi: int = 300,
    png_optimize: bool = False,
    png_quantize: bool = False,
) -> bytes:
    fmt = (fmt or "png").lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in {"png", "jpg", "webp", "tif", "tiff"}:
        raise UserError(f"不支持的图片格式：{fmt}")
    if fmt in {"jpg", "tif", "tiff"}:
        if bgra.shape[2] == 4 and background is None:
            background = (255, 255, 255)
    if background is not None:
        bgra = composite_bgra(bgra, background)

    rgba = cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGBA)
    image = Image.fromarray(rgba)
    buf = io.BytesIO()
    quality = int(clamp(quality, 30, 100))
    if fmt == "jpg":
        image.convert("RGB").save(
            buf, format="JPEG", quality=quality, subsampling=0,
            optimize=False, progressive=True, dpi=(dpi, dpi),
        )
    elif fmt == "png":
        if png_quantize:
            image = image.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
        image.save(
            buf, format="PNG",
            compress_level=9 if png_optimize else 6,
            optimize=png_optimize,
            dpi=(dpi, dpi),
        )
    elif fmt == "webp":
        image.save(buf, format="WEBP", quality=quality, method=4, dpi=(dpi, dpi))
    else:
        image.save(buf, format="TIFF", compression="tiff_lzw", dpi=(dpi, dpi))
    return buf.getvalue()


def write_bytes(path: str | Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save_source_preview(path: str | Path, source_bgr: np.ndarray, max_side: int = 2000) -> None:
    height, width = source_bgr.shape[:2]
    image = source_bgr
    longest = max(height, width)
    if longest > max_side:
        scale = max_side / float(longest)
        image = cv2.resize(
            source_bgr,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=88, optimize=True)
    write_bytes(path, buf.getvalue())
