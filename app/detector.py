"""Paper edge detection and rotation (deskew) estimation.

The detector is designed for a light sheet photographed or scanned on a
noticeably darker surface.  It thresholds luminance, removes thin dark ink
lines from the *background* candidate, floods the background from the image
border and keeps the largest remaining region.  That makes closed comic panel
borders part of the sheet instead of turning each panel into a separate
"paper" candidate.  The real, possibly torn, outline is then traced as a
polygon.

Orientation is estimated from long straight content lines (panel borders,
text baselines) with a fallback to the sheet outline's minimum-area rectangle.
"""

from __future__ import annotations

import math
import time
from typing import Any

import cv2
import numpy as np

from .util import UserError, clamp


DEFAULT_PARAMS: dict[str, Any] = {
    "threshold": None,     # None = automatic (Otsu)
    "invert": False,       # detect a dark sheet on a light surface
    "simplify": 2.0,       # px, approxPolyDP tolerance
    "expand": 0,           # px, negative trims dark fringe; positive adds safety
    "feather": 0.8,        # px, alpha softening
    "detect_holes": False, # cut large enclosed holes out of the sheet
}

MAX_DETECT_SIDE = 5200
MAX_SKEW_SIDE = 1600
DEFAULT_ALIGN = "all"
ALIGN_EDGES = ("top", "bottom", "left", "right", "all")


def normalize_align(value: Any) -> str:
    return value if value in ALIGN_EDGES else DEFAULT_ALIGN


def normalize_params(raw: dict[str, Any] | None, base: dict[str, Any] | None = None) -> dict[str, Any]:
    params = dict(DEFAULT_PARAMS)
    if base:
        params.update({k: v for k, v in base.items() if k in DEFAULT_PARAMS})
    if raw:
        for key in DEFAULT_PARAMS:
            if key in raw:
                params[key] = raw[key]
    threshold = params.get("threshold")
    if threshold in ("", "auto", None):
        params["threshold"] = None
    else:
        try:
            params["threshold"] = float(clamp(float(threshold), 1, 254))
        except (TypeError, ValueError):
            params["threshold"] = None
    params["invert"] = bool(params.get("invert", False))
    params["detect_holes"] = bool(params.get("detect_holes", False))
    params["simplify"] = float(clamp(float(params.get("simplify", 2.0)), 0.0, 20.0))
    params["expand"] = int(round(clamp(float(params.get("expand", 0)), -30, 30)))
    params["feather"] = float(clamp(float(params.get("feather", 0.8)), 0.0, 8.0))
    return params


def _warn(code: str, message: str, level: str = "warning") -> dict[str, str]:
    return {"code": code, "message": message, "level": level}


def _largest_component(mask: np.ndarray) -> tuple[np.ndarray, int, list[int]]:
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return np.zeros_like(mask), 0, []
    areas = stats[1:, cv2.CC_STAT_AREA].astype(np.int64)
    largest = 1 + int(np.argmax(areas))
    paper = np.where(labels == largest, 255, 0).astype(np.uint8)
    others = [int(v) for i, v in enumerate(areas) if i + 1 != largest]
    return paper, int(stats[largest, cv2.CC_STAT_AREA]), others


def _normal_paper_mask(binary: np.ndarray) -> tuple[np.ndarray, int, list[int]]:
    """Flood the dark backdrop from the border; keep its complement.

    Thin dark lines (ink, panel borders) are removed from the *background*
    candidate first, so they cannot leak the outside into a closed panel.
    Wide dark bites and tears remain background and are therefore preserved.
    """
    height, width = binary.shape[:2]
    dark = cv2.bitwise_not(binary)
    kernel_size = max(7, int(round(min(height, width) * 0.0025)) | 1)
    opened = cv2.morphologyEx(
        dark, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)),
    )

    pad = 2
    padded = np.full((height + pad * 2, width + pad * 2), 255, dtype=np.uint8)
    padded[pad:pad + height, pad:pad + width] = opened
    filled = padded.copy()
    flood_mask = np.zeros((padded.shape[0] + 2, padded.shape[1] + 2), dtype=np.uint8)
    cv2.floodFill(filled, flood_mask, (0, 0), 128, flags=8)
    outside = (filled == 128)[pad:pad + height, pad:pad + width]
    inside = np.where(outside, 0, 255).astype(np.uint8)
    paper, area, others = _largest_component(inside)
    if area <= 0:
        return paper, area, others

    # A thin drawn frame (comic border, green/black pen) can split the sheet
    # into an "inside the frame" component and an outer paper margin.  Both are
    # paper.  Bridge small gaps between the main component and nearby
    # components; wide tears/notches and far-away specks stay untouched.
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(inside, connectivity=8)
    bridge = cv2.dilate(
        (paper > 0).astype(np.uint8),
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)),
        iterations=1,
    )
    merged = paper.copy()
    for index in range(1, count):
        if int(stats[index, cv2.CC_STAT_AREA]) < 40:
            continue
        component = (labels == index).astype(np.uint8) * 255
        if component[bridge > 0].any():
            merged = cv2.bitwise_or(merged, component)
    if int((merged > 0).sum()) > area:
        area = int((merged > 0).sum())
    return merged, area, others


def _content_line_skew(image_bgr: np.ndarray, polygon: list[list[float]] | None = None) -> dict[str, Any] | None:
    """Estimate rotation from long straight lines that follow the page."""
    height, width = image_bgr.shape[:2]
    scale = min(1.0, MAX_SKEW_SIDE / float(max(height, width)))
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    if scale < 1.0:
        gray = cv2.resize(gray, (int(round(width * scale)), int(round(height * scale))),
                          interpolation=cv2.INTER_AREA)
    small_h, small_w = gray.shape[:2]
    edges = cv2.Canny(gray, 50, 160)
    min_length = max(24, int(round(min(small_h, small_w) * 0.12)))
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 720, threshold=55,
        minLineLength=min_length, maxLineGap=12,
    )
    if lines is None:
        return None
    poly = None
    if polygon and len(polygon) >= 3:
        poly = np.asarray(polygon, dtype=np.float32) / (scale if scale else 1.0)
    values: list[tuple[float, float]] = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        dx = float(x2 - x1)
        dy = float(y2 - y1)
        length = math.hypot(dx, dy)
        if length < min_length:
            continue
        angle = math.degrees(math.atan2(dy, dx))
        delta = ((angle + 45.0) % 90.0) - 45.0
        if abs(delta) > 30.0:
            continue
        if poly is not None:
            inside = 0
            for t in (0.1, 0.3, 0.5, 0.7, 0.9):
                point = (float(x1) + dx * t, float(y1) + dy * t)
                if cv2.pointPolygonTest(poly, point, False) >= -1.0:
                    inside += 1
            if inside < 3:
                continue
        values.append((delta, length))
    if len(values) < 4:
        return None

    # Weighted histogram; smooth so near-identical line angles reinforce.
    bins = np.arange(-30.0, 30.0001, 0.1)
    histogram = np.zeros(len(bins) - 1, dtype=np.float64)
    for delta, length in values:
        index = int(np.searchsorted(bins, delta)) - 1
        if 0 <= index < len(histogram):
            histogram[index] += length * length
    if histogram.max() <= 0:
        return None
    smooth = np.convolve(histogram, np.ones(5) / 5.0, mode="same")
    best = int(np.argmax(smooth))
    center = (bins[best] + bins[best + 1]) / 2.0
    near = [delta for delta, _length in values if abs(delta - center) <= 0.55]
    total = sum(length for _delta, length in values)
    consensus = sum(length for delta, length in values if abs(delta - center) <= 0.55) / (total or 1.0)
    if len(near) < 4:
        return None
    angle = -float(np.median(near))
    return {
        "angle": round(angle, 2),
        "confidence": round(float(clamp(consensus, 0.0, 1.0)), 3),
        "lines": len(values),
        "near_lines": len(near),
        "source": "content",
    }


def _order_quad(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    total = points.sum(axis=1)
    diff = points[:, 0] - points[:, 1]
    tl = points[int(np.argmin(total))]
    br = points[int(np.argmax(total))]
    tr = points[int(np.argmax(diff))]
    bl = points[int(np.argmin(diff))]
    ordered = np.array([tl, tr, br, bl], dtype=np.float32)
    # Degenerate extreme-point selection: fall back to angular order.
    if len(np.unique(ordered, axis=0)) < 4:
        center = points.mean(axis=0)
        angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
        points = points[np.argsort(angles)]
        start = int(np.argmin(points.sum(axis=1)))
        ordered = np.roll(points, -start, axis=0)
    return ordered


def _side_lengths(quad: np.ndarray) -> tuple[float, float, float, float]:
    top = float(np.linalg.norm(quad[1] - quad[0]))
    right = float(np.linalg.norm(quad[2] - quad[1]))
    bottom = float(np.linalg.norm(quad[3] - quad[2]))
    left = float(np.linalg.norm(quad[0] - quad[3]))
    return top, right, bottom, left


def _angle_between(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    cosine = float(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def _parallel_deviation(a: np.ndarray, b: np.ndarray) -> float:
    angle = _angle_between(a, b)
    return min(angle, abs(180.0 - angle))


def _segment_paper_rect(polygon: list[list[float]], correction: float
                        ) -> list[list[float]] | None:
    """Robust rectangle from the long, straight contour segments.

    Both tears (inward) and lifted/curled fibres (outward) produce short,
    irregular segments.  The intact paper sides are long and parallel, so a
    length-weighted median of long segment offsets ignores both kinds of
    damage.  This is stricter than a convex-hull bounding box when the top or
    a corner has burst outward.
    """
    if not polygon or len(polygon) < 3:
        return None
    points = np.asarray(polygon, dtype=np.float64)
    segments = np.roll(points, -1, axis=0) - points
    lengths = np.linalg.norm(segments, axis=1)
    if not len(lengths) or lengths.max() < 8.0:
        return None
    mids = (points + np.roll(points, -1, axis=0)) / 2.0
    theta = math.radians(-correction)
    u = np.array([math.cos(theta), math.sin(theta)], dtype=np.float64)
    v = np.array([-math.sin(theta), math.cos(theta)], dtype=np.float64)
    pu = points @ u
    pv = points @ v
    mid_u = mids @ u
    mid_v = mids @ v
    center_u = (float(pu.min()) + float(pu.max())) / 2.0
    center_v = (float(pv.min()) + float(pv.max())) / 2.0
    paper_angle = -correction
    groups: dict[str, list[tuple[float, float]]] = {"top": [], "bottom": [], "left": [], "right": []}
    max_length = float(lengths.max())
    for index, (start, end, length) in enumerate(zip(points, np.roll(points, -1, axis=0), lengths)):
        if length < max(8.0, max_length * 0.035):
            continue
        angle = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))
        relative = (angle - paper_angle) % 180.0
        horizontal = min(relative, 180.0 - relative) <= 18.0
        vertical = abs(relative - 90.0) <= 18.0
        if horizontal:
            side = "top" if mid_v[index] < center_v else "bottom"
            groups[side].append((float(mid_v[index]), float(length) ** 2))
        elif vertical:
            side = "left" if mid_u[index] < center_u else "right"
            groups[side].append((float(mid_u[index]), float(length) ** 2))
    if any(len(values) < 2 for values in groups.values()):
        return None
    offsets = {}
    for side, values in groups.items():
        arr = np.asarray(values, dtype=np.float64)
        offsets[side] = _weighted_median(arr[:, 0], arr[:, 1])
    # Keep the four lines parallel to the estimated paper axes.
    umin, umax = offsets["left"], offsets["right"]
    vmin, vmax = offsets["top"], offsets["bottom"]
    if umax <= umin or vmax <= vmin:
        return None
    corners = []
    for a, b in ((umin, vmin), (umax, vmin), (umax, vmax), (umin, vmax)):
        point = u * a + v * b
        corners.append([round(float(point[0]), 1), round(float(point[1]), 1)])
    return corners


def _rect_from_offsets(u: np.ndarray, v: np.ndarray,
                       umin: float, umax: float, vmin: float, vmax: float) -> list[list[float]]:
    corners = []
    for a, b in ((umin, vmin), (umax, vmin), (umax, vmax), (umin, vmax)):
        point = u * a + v * b
        corners.append([round(float(point[0]), 1), round(float(point[1]), 1)])
    return corners


def _rect_plausible(rect: list[list[float]], polygon: list[list[float]]) -> bool:
    if len(rect) != 4 or not polygon:
        return False
    rect_arr = np.asarray(rect, dtype=np.float32)
    points = np.asarray(polygon, dtype=np.float32)
    hull = cv2.convexHull(points)
    hull_area = max(1.0, float(cv2.contourArea(hull)))
    rect_area = float(cv2.contourArea(rect_arr))
    inside = sum(
        1 for point in points
        if cv2.pointPolygonTest(rect_arr, (float(point[0]), float(point[1])), False) >= 0
    )
    return (inside >= max(4, int(len(points) * 0.70))
            and rect_area >= hull_area * 0.72)


def detect_paper_rect(polygon: list[list[float]]) -> list[list[float]]:
    """Reconstruct the rectangle the sheet had before its edges were damaged.

    Damage is mostly subtractive, but torn/curled corners can also lift a few
    fibres outward.  Prefer long straight side segments (robust to both) and
    fall back to the convex-hull supporting rectangle if the contour is too
    fragmentary.
    """
    if not polygon or len(polygon) < 3:
        return []
    correction = estimate_deskew_angle(polygon)
    robust = _segment_paper_rect(polygon, correction)
    if robust is not None and _rect_plausible(robust, polygon):
        return robust
    points = np.asarray(polygon, dtype=np.float32).reshape(-1, 1, 2)
    hull = cv2.convexHull(points).reshape(-1, 2).astype(np.float64)
    if len(hull) < 3:
        return []
    theta = math.radians(-correction)
    u = np.array([math.cos(theta), math.sin(theta)], dtype=np.float64)
    v = np.array([-math.sin(theta), math.cos(theta)], dtype=np.float64)
    pu = hull @ u
    pv = hull @ v
    return _rect_from_offsets(
        u, v, float(pu.min()) - 0.5, float(pu.max()) + 0.5,
        float(pv.min()) - 0.5, float(pv.max()) + 0.5,
    )


def detect_quad(polygon: list[list[float]]) -> dict[str, Any]:
    """Find the four paper corners, falling back to the minimum-area box."""
    if not polygon or len(polygon) < 3:
        return {"quad": [], "near_rect": True, "quad_source": "none"}
    points = np.asarray(polygon, dtype=np.float32).reshape(-1, 1, 2)
    hull = cv2.convexHull(points)
    perimeter = cv2.arcLength(hull, True)
    quad = None
    source = "contour"
    for fraction in (0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.07, 0.1):
        approx = cv2.approxPolyDP(hull, max(1.0, perimeter * fraction), True)
        if len(approx) == 4:
            quad = approx.reshape(4, 2).astype(np.float32)
            break
    if quad is None:
        _center, _size, _angle = cv2.minAreaRect(points)
        quad = cv2.boxPoints((_center, _size, _angle)).astype(np.float32)
        source = "box"
    quad = _order_quad(quad)
    top, right, bottom, left = _side_lengths(quad)
    v_top = quad[1] - quad[0]
    v_right = quad[2] - quad[1]
    v_bottom = quad[3] - quad[2]
    v_left = quad[0] - quad[3]
    angle_dev = max(
        _parallel_deviation(v_top, v_bottom),
        _parallel_deviation(v_left, v_right),
        abs(_angle_between(v_top, v_right) - 90.0),
        abs(_angle_between(v_bottom, v_left) - 90.0),
    )
    length_ratios = (
        abs(top - bottom) / max(1.0, max(top, bottom)),
        abs(left - right) / max(1.0, max(left, right)),
    )
    min_length_dev = min(length_ratios)
    near_rect = source == "box" or (angle_dev < 8.0 and min_length_dev < 0.05)
    return {
        "quad": [[round(float(x), 1), round(float(y), 1)] for x, y in quad],
        "near_rect": bool(near_rect),
        "quad_source": source,
        "quad_aspect": round((top + bottom) / max(1.0, left + right), 4),
        "quad_angle_dev": round(float(angle_dev), 2),
    }


def _paper_colour(image_bgr: np.ndarray, mask: np.ndarray, threshold: float) -> str:
    if mask.size == 0:
        return "#ffffff"
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    selected = (mask > 0) & (gray >= max(70.0, threshold * 0.82))
    pixels = image_bgr[selected]
    if pixels.size == 0:
        pixels = image_bgr[mask > 0]
    if pixels.size == 0:
        return "#ffffff"
    b, g, r = (int(v) for v in np.median(pixels, axis=0))
    return f"#{r:02x}{g:02x}{b:02x}"


def detect_holes(gray: np.ndarray, paper_mask: np.ndarray, threshold: float,
                 invert: bool, scale: float) -> list[list[list[float]]]:
    """Find large, thick dark regions enclosed by the sheet.

    Thin ink lines and panel borders are ignored; only regions with enough
    area and thickness become cut-out holes.  This is opt-in because a filled
    black drawing can look like a physical hole.
    """
    paper_area = int((paper_mask > 0).sum())
    if paper_area <= 0:
        return []
    if invert:
        candidate = (gray > threshold) & (paper_mask > 0)
    else:
        candidate = (gray < threshold) & (paper_mask > 0)
    candidate = candidate.astype(np.uint8) * 255
    if int(candidate.sum()) == 0:
        return []
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(candidate, connectivity=8)
    if count <= 1:
        return []
    min_area = max(24, int(paper_area * 0.0015))
    min_thickness = max(2.5, min(gray.shape[:2]) * 0.0015)
    holes: list[list[list[float]]] = []
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        x, y, w, h = (int(v) for v in stats[index, :4])
        component = (labels[y:y + h, x:x + w] == index).astype(np.uint8)
        if int(component.sum()) == 0:
            continue
        thickness = float(cv2.distanceTransform(component, cv2.DIST_L2, 5).max())
        if thickness < min_thickness:
            continue
        contours, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        contour = contour + np.array([[x, y]], dtype=np.int32)
        approx = cv2.approxPolyDP(contour, 1.5, True).reshape(-1, 2).astype(np.float32)
        if len(approx) < 3:
            continue
        if scale < 1.0:
            approx = approx / scale
        holes.append([
            [round(float(clamp(px, 0, gray.shape[1] * (1.0 / scale if scale else 1) - 1)), 1),
             round(float(clamp(py, 0, gray.shape[0] * (1.0 / scale if scale else 1) - 1)), 1)]
            for px, py in approx
        ])
    return holes


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    values = values[order]
    weights = weights[order]
    cumulative = np.cumsum(weights)
    return float(values[int(np.searchsorted(cumulative, cumulative[-1] / 2.0))])


def _contour_edge_skew(polygon: list[list[float]]) -> float | None:
    """Estimate paper orientation from long straight edges of the outline.

    minAreaRect is easily nudged by a torn corner or a protruding fibre.  The
    long *segments* of the simplified contour are a better vote: a straight
    paper side contributes one long vector, while a torn side contributes many
    short vectors of mixed direction.
    """
    if not polygon or len(polygon) < 3:
        return None
    points = np.asarray(polygon, dtype=np.float64)
    segments = np.roll(points, -1, axis=0) - points
    lengths = np.linalg.norm(segments, axis=1)
    if not len(lengths) or lengths.max() < 8.0:
        return None
    angles = np.degrees(np.arctan2(segments[:, 1], segments[:, 0]))
    deltas = ((angles + 45.0) % 90.0) - 45.0
    keep = (lengths >= max(8.0, float(lengths.max()) * 0.04)) & (np.abs(deltas) <= 30.0)
    if int(keep.sum()) < 2:
        return None
    weighted = lengths[keep] ** 2
    if float(weighted.sum()) < (float(lengths.max()) ** 2) * 0.8:
        return None
    return round(-_weighted_median(deltas[keep], weighted), 3)


def _side_line_skew(polygon: list[list[float]], side: str) -> dict[str, Any] | None:
    """Fit one paper side and return its clockwise correction.

    A torn corner must not decide the rotation of the whole page, so points are
    sampled from the middle of the requested side only and fitted with a Huber
    loss.  The caller still cross-checks this against the all-edge estimate.
    """
    if side not in ("top", "bottom", "left", "right") or not polygon or len(polygon) < 3:
        return None
    points = np.asarray(polygon, dtype=np.float64)
    try:
        rect = cv2.minAreaRect(points.astype(np.float32).reshape(-1, 1, 2))
        box = _order_quad(cv2.boxPoints(rect).astype(np.float32)).astype(np.float64)
    except cv2.error:
        return None
    tl, tr, br, bl = box
    u = (tr - tl) / max(1e-6, float(np.linalg.norm(tr - tl)))
    v = (bl - tl) / max(1e-6, float(np.linalg.norm(bl - tl)))
    pu = (points - tl) @ u
    pv = (points - tl) @ v
    umin, umax = float(pu.min()), float(pu.max())
    vmin, vmax = float(pv.min()), float(pv.max())
    width = max(1e-6, umax - umin)
    height = max(1e-6, vmax - vmin)
    margin_u = width * 0.10
    margin_v = height * 0.10
    bands = {
        "top": (pv < vmin + margin_v) & (pu > umin + margin_u) & (pu < umax - margin_u),
        "bottom": (pv > vmax - margin_v) & (pu > umin + margin_u) & (pu < umax - margin_u),
        "left": (pu < umin + margin_u) & (pv > vmin + margin_v) & (pv < vmax - margin_v),
        "right": (pu > umax - margin_u) & (pv > vmin + margin_v) & (pv < vmax - margin_v),
    }
    chosen = points[bands[side]]
    if len(chosen) < 4:
        return None
    try:
        vx, vy, x0, y0 = cv2.fitLine(
            chosen.reshape(-1, 1, 2).astype(np.float32),
            cv2.DIST_HUBER, 0, 0.01, 0.01,
        ).ravel()
    except cv2.error:
        return None
    angle = math.degrees(math.atan2(float(vy), float(vx)))
    expected = 0.0 if side in ("top", "bottom") else 90.0
    delta = ((angle - expected + 45.0) % 90.0) - 45.0
    # Residual gives a cheap reliability signal (torn/curved sides are noisy).
    nx, ny = -float(vy), float(vx)
    distances = np.abs((chosen[:, 0] - float(x0)) * nx + (chosen[:, 1] - float(y0)) * ny)
    rms = float(np.sqrt(np.mean(distances ** 2))) if len(distances) else 0.0
    tolerance = max(2.0, min(width, height) * 0.012)
    confidence = float(clamp(0.95 - rms / (tolerance * 2.0) + min(len(chosen), 30) / 120.0, 0.2, 0.95))
    return {
        "angle": round(-delta, 3),
        "confidence": round(confidence, 3),
        "points": int(len(chosen)),
        "rms": round(rms, 2),
    }


def estimate_deskew_angle(polygon: list[list[float]]) -> float:
    """Outline-only fallback, in clockwise degrees."""
    if not polygon or len(polygon) < 3:
        return 0.0
    points = np.asarray(polygon, dtype=np.float32).reshape(-1, 1, 2)
    _center, _size, angle = cv2.minAreaRect(points)
    if angle > 45:
        angle -= 90
    if angle <= -45:
        angle += 90
    estimate = round(float(clamp(-angle, -45, 45)), 2)
    edge_estimate = _contour_edge_skew(polygon)
    if edge_estimate is not None:
        # Only let the edge vote override minAreaRect when they broadly agree;
        # a contour made of unrelated diagonal segments should not win.
        if abs(edge_estimate - estimate) <= 1.5:
            estimate = edge_estimate
    if abs(estimate) < 0.10:
        return 0.0
    return round(float(clamp(estimate, -45, 45)), 2)


def estimate_skew(image_bgr: np.ndarray, polygon: list[list[float]],
                  align: str = "all") -> dict[str, Any]:
    align = normalize_align(align)
    outline = estimate_deskew_angle(polygon)
    content = _content_line_skew(image_bgr, polygon)
    result: dict[str, Any]
    if content:
        # Content lines are preferred only when they do not contradict the
        # sheet outline by a suspicious amount (hatching can be diagonal).
        difference = abs(((content["angle"] - outline + 45.0) % 90.0) - 45.0)
        strong = (content["confidence"] >= 0.80
                  and content["near_lines"] >= 8
                  and difference <= 15.0)
        consistent = content["confidence"] >= 0.55 and difference <= 3.0
        if content["near_lines"] >= 5 and (strong or consistent):
            result = content
        else:
            result = {
                "angle": outline,
                "confidence": 0.35,
                "lines": 0,
                "near_lines": 0,
                "source": "outline",
            }
    else:
        result = {
            "angle": outline,
            "confidence": 0.35,
            "lines": 0,
            "near_lines": 0,
            "source": "outline",
        }

    if align in ("top", "bottom", "left", "right"):
        side = _side_line_skew(polygon, align)
        if side is not None:
            difference = abs(((side["angle"] - result["angle"] + 45.0) % 90.0) - 45.0)
            # If the requested side is torn, curved, or disagrees with the
            # other edges, fall back to the all-edge estimate instead of
            # forcing a visibly wrong rotation.
            if difference <= 1.2:
                return {
                    "angle": round(float(side["angle"]), 2),
                    "confidence": round(float(side.get("confidence", 0.5)), 3),
                    "lines": int(side.get("points", 0)),
                    "near_lines": int(side.get("points", 0)),
                    "source": align,
                    "align_used": align,
                }
    result["align_used"] = "all"
    result["align_requested"] = align
    return result


def detect_paper(image_bgr: np.ndarray, raw_params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a detection dict; raise UserError when no sheet can be found."""
    started = time.perf_counter()
    params = normalize_params(raw_params)
    height, width = image_bgr.shape[:2]
    if height < 16 or width < 16:
        raise UserError("图片尺寸过小，无法识别纸张。")

    scale = min(1.0, MAX_DETECT_SIDE / float(max(height, width)))
    if scale < 1.0:
        work = cv2.resize(
            image_bgr,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        work = image_bgr

    work_h, work_w = work.shape[:2]
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    if float(gray.std()) < 2.0:
        raise UserError("图片几乎是纯色，无法找到纸张边缘。")
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    if params["threshold"] is None:
        threshold, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        threshold = float(threshold)
    else:
        threshold = float(params["threshold"])
        _, binary = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)

    if params["invert"]:
        binary = cv2.bitwise_not(binary)

    binary = cv2.morphologyEx(
        binary, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )

    if params["invert"]:
        # Simple dark-sheet mode; closed light panel lines are rare here.
        paper_mask, paper_area, others = _largest_component(binary)
    else:
        paper_mask, paper_area, others = _normal_paper_mask(binary)

    total_px = float(work_w * work_h)
    area_ratio = paper_area / total_px
    if paper_area <= 0 or area_ratio < 0.004:
        raise UserError(
            "没有检测到纸张。请让纸张与背景有较明显的明暗差异，"
            "或手动调整识别阈值。"
        )

    contours, _ = cv2.findContours(paper_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        raise UserError("纸张轮廓提取失败，请尝试调整识别阈值。")
    contour = max(contours, key=cv2.contourArea)

    epsilon = float(params["simplify"])
    approx = cv2.approxPolyDP(contour, epsilon, True)
    if len(approx) > 3000:
        approx = cv2.approxPolyDP(contour, max(1.0, epsilon), True)
    if len(approx) < 3:
        approx = cv2.approxPolyDP(contour, max(0.5, epsilon / 2.0), True)
    if len(approx) < 3:
        raise UserError("纸张轮廓过于简单，请降低“边缘简化”数值后重试。")

    points = approx.reshape(-1, 2).astype(np.float32)
    if scale < 1.0:
        points = points / scale
    points[:, 0] = np.clip(points[:, 0], 0, width - 1)
    points[:, 1] = np.clip(points[:, 1], 0, height - 1)
    polygon = [[round(float(x), 1), round(float(y), 1)] for x, y in points]

    x, y, w, h = cv2.boundingRect(contour)
    if scale < 1.0:
        x, y, w, h = (int(round(v / scale)) for v in (x, y, w, h))
    bbox = [x, y, w, h]

    inv_scale2 = 1.0 / (scale * scale) if scale < 1 else 1.0
    contour_area = float(abs(cv2.contourArea(contour))) * inv_scale2
    hull = cv2.convexHull(contour)
    hull_area = float(abs(cv2.contourArea(hull))) * inv_scale2
    solidity = float(contour_area / hull_area) if hull_area > 0 else 1.0

    paper_pixels = gray[paper_mask > 0]
    paper_mean = float(paper_pixels.mean()) if paper_pixels.size else 0.0

    multiple = any(value > paper_area * 0.25 for value in others)
    skew = estimate_skew(image_bgr, polygon)
    quad = detect_quad(polygon)
    paper_rect = detect_paper_rect(polygon)
    paper_color = _paper_colour(work, paper_mask, threshold)
    holes = (detect_holes(gray, paper_mask, threshold, params["invert"], scale)
             if params.get("detect_holes") else [])
    edge_pad = max(2.0, 0.0015 * max(width, height))
    touches_edge = bool(
        x <= edge_pad or y <= edge_pad
        or x + w >= width - edge_pad
        or y + h >= height - edge_pad
    )

    warnings: list[dict[str, str]] = []
    if multiple:
        warnings.append(_warn(
            "multiple_papers",
            "画面中似乎不止一张纸，目前只裁切面积最大的那一张。",
            "info",
        ))
    if touches_edge:
        warnings.append(_warn(
            "touches_edge",
            "纸张边缘贴近画面边界，可能有内容被拍漏；建议检查裁切结果。",
        ))
    if area_ratio > 0.985:
        warnings.append(_warn(
            "fills_frame",
            "纸张几乎占满画面，自动边缘可能是画面边界。",
        ))
    if area_ratio < 0.02:
        warnings.append(_warn(
            "small_area",
            "纸张在画面中很小，裁切精度可能受影响。",
        ))
    if threshold < 35 or threshold > 220:
        warnings.append(_warn(
            "low_contrast",
            "纸张与背景的对比度偏低，建议检查边缘或手动调整阈值。",
        ))
    if paper_mean < 95:
        warnings.append(_warn(
            "dark_paper",
            "纸张整体偏暗，自动识别可能不稳定。",
            "info",
        ))
    if abs(skew["angle"]) >= 1.0 and skew["confidence"] < 0.55:
        angle_text = f"{skew['angle']:.1f}"
        warnings.append(_warn(
            "skew_uncertain",
            f"纸张可能倾斜 {angle_text}°，但可参考的直线较少，建议在编辑器中确认。",
            "info",
        ))

    confidence = 1.0
    if area_ratio < 0.04:
        confidence -= 0.20
    if touches_edge:
        confidence -= 0.15
    if solidity < 0.82:
        confidence -= 0.08
    if threshold < 35 or threshold > 220:
        confidence -= 0.20
    if multiple:
        confidence -= 0.10
    confidence = float(clamp(confidence, 0.05, 1.0))

    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    return {
        "polygon": polygon,
        "threshold": round(float(threshold), 1),
        "auto_threshold": params["threshold"] is None,
        "area": paper_area,
        "area_ratio": round(float(area_ratio), 4),
        "solidity": round(float(solidity), 4),
        "bbox": bbox,
        "paper_mean": round(float(paper_mean), 1),
        "warnings": warnings,
        "confidence": round(confidence, 2),
        "components": int(len(others) + 1),
        "skew_angle": skew["angle"],
        "skew_confidence": skew["confidence"],
        "skew_source": skew["source"],
        "skew_lines": skew["lines"],
        "quad": quad["quad"],
        "quad_near_rect": quad["near_rect"],
        "quad_source": quad["quad_source"],
        "quad_aspect": quad.get("quad_aspect"),
        "quad_angle_dev": quad.get("quad_angle_dev"),
        "paper_rect": paper_rect,
        "paper_color": paper_color,
        "holes": holes,
        "elapsed_ms": elapsed_ms,
    }
