"""Core tests for PaperTrim.  All sample images are generated, never bundled."""

from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app.detector import detect_paper, detect_regions
from app.exporters import run_export
from app.imaging import (
    build_mask, encode_bgra, load_bgr, read_image_size, render_record,
    apply_desaturate,
)
from app.store import Store
from app.projects import ProjectManager


def make_scan(path: Path, width: int = 1400, height: int = 1000,
              dark_paper: bool = False, rotate: float = 0.0) -> dict:
    """Create a synthetic irregular sheet on a contrasting backdrop."""
    background = 245 if dark_paper else 18
    paper = 60 if dark_paper else 232
    image = np.full((height, width, 3), background, dtype=np.uint8)
    polygon = np.array([
        [110, 60], [520, 45], [610, 75], [830, 55], [1270, 95],
        [1300, 330], [1255, 610], [1280, 930], [940, 955],
        [860, 920], [610, 960], [470, 925], [160, 950],
        [120, 700], [145, 430], [80, 260],
    ], dtype=np.int32)
    cv2.fillPoly(image, [polygon], (paper, paper, paper), lineType=cv2.LINE_AA)
    ink = (245, 245, 245) if dark_paper else (25, 25, 25)
    for i in range(7):
        y = 120 + i * 80
        cv2.line(image, (150, y), (300 + i * 25, y + 35), ink, 3, cv2.LINE_AA)
    cv2.line(image, (1180, 150), (1215, 520), ink, 4, cv2.LINE_AA)
    cv2.putText(image, "SAMPLE", (360, 430), cv2.FONT_HERSHEY_SIMPLEX,
                2.0, ink, 4, cv2.LINE_AA)
    # bright dust outside the sheet must not become the paper
    cv2.line(image, (30, 180), (55, 260), (background + 30,) * 3, 2, cv2.LINE_AA)
    if rotate:
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), rotate, 1.0)
        image = cv2.warpAffine(image, matrix, (width, height),
                               borderMode=cv2.BORDER_CONSTANT,
                               borderValue=(background,) * 3)
    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 94])
    assert ok
    path.write_bytes(buf.tobytes())
    return {"polygon": polygon, "width": width, "height": height}


def make_panel_scan(path: Path, rotate: float = 8.0) -> None:
    """Synthetic comic page with straight panel borders, rotated by `rotate`."""
    width, height = 1400, 1000
    image = np.full((height, width, 3), 18, dtype=np.uint8)
    polygon = np.array([
        [90, 55], [520, 45], [680, 70], [1300, 60], [1320, 300],
        [1280, 650], [1300, 940], [800, 955], [600, 925], [150, 940], [105, 600],
    ], dtype=np.int32)
    cv2.fillPoly(image, [polygon], (230, 230, 226), lineType=cv2.LINE_AA)
    ink = (30, 30, 30)
    left, top, right, bottom = 220, 150, 1180, 830
    cv2.rectangle(image, (left, top), (right, bottom), ink, 4, cv2.LINE_AA)
    cv2.line(image, ((left + right) // 2, top), ((left + right) // 2, bottom), ink, 4, cv2.LINE_AA)
    cv2.line(image, (left, (top + bottom) // 2), (right, (top + bottom) // 2), ink, 4, cv2.LINE_AA)
    cv2.putText(image, "PANEL", (300, 360), cv2.FONT_HERSHEY_SIMPLEX,
                1.4, ink, 3, cv2.LINE_AA)
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), rotate, 1.0)
    image = cv2.warpAffine(image, matrix, (width, height),
                           borderMode=cv2.BORDER_CONSTANT, borderValue=(18, 18, 18))
    path.write_bytes(cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 94])[1].tobytes())


class DetectionTests(unittest.TestCase):
    def test_irregular_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.jpg"
            make_scan(path)
            image = load_bgr(path)
            result = detect_paper(image)
            self.assertGreaterEqual(len(result["polygon"]), 10)
            self.assertGreater(result["area_ratio"], 0.5)
            self.assertLess(result["area_ratio"], 0.9)
            mask = build_mask(image.shape[:2], result["polygon"], expand=0, feather=0)
            # Ink that was drawn near the paper boundary must be inside.
            self.assertGreater(int(mask[120:680, 150:330].min()), 200)
            self.assertGreater(int(mask[200:500, 1150:1220].min()), 200)

    def test_tilted_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tilted.jpg"
            make_scan(path, rotate=8.0)
            result = detect_paper(load_bgr(path))
            self.assertTrue(0.3 < result["area_ratio"] < 0.9)
            self.assertGreaterEqual(len(result["polygon"]), 4)

    def test_dark_paper_inverted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dark.jpg"
            make_scan(path, dark_paper=True)
            result = detect_paper(load_bgr(path), {"invert": True})
            self.assertGreater(result["area_ratio"], 0.5)
            self.assertLess(result["area_ratio"], 0.9)

    def test_skew_estimation_and_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "panel.jpg"
            make_panel_scan(path, rotate=8.0)
            result = detect_paper(load_bgr(path))
            self.assertEqual(result["skew_source"], "content")
            self.assertAlmostEqual(result["skew_angle"], 8.0, delta=0.8)
            # Closed panel borders must not turn a single panel into "the paper".
            self.assertGreater(result["area_ratio"], 0.55)
            self.assertGreater(result["bbox"][2], 900)

            store = Store(Path(tmp) / "data")
            record, reason = store.import_file(path, original_name=path.name)
            self.assertIsNotNone(record, reason)
            detail = store.detect(record["id"])
            detail = store.deskew(record["id"], force=False)
            self.assertAlmostEqual(detail["fine_angle"], 8.0, delta=0.8)

    def test_exif_orientation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exif.jpg"
            image = np.full((600, 800, 3), 18, dtype=np.uint8)
            cv2.fillPoly(image, [np.array([[60, 50], [740, 50], [740, 550], [60, 550]], np.int32)],
                         (230, 230, 230))
            pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            exif = pil.getexif()
            exif[274] = 6
            pil.save(path, "JPEG", quality=92, exif=exif)
            self.assertEqual(read_image_size(path), (600, 800))
            self.assertEqual(load_bgr(path).shape[:2], (800, 600))
            result = detect_paper(load_bgr(path))
            self.assertGreater(result["area_ratio"], 0.5)

    def test_inner_frame_does_not_crop_margin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inner_frame.jpg"
            image = np.full((1000, 1400, 3), 8, dtype=np.uint8)
            cv2.rectangle(image, (100, 100), (1300, 900), (220, 220, 220), -1)
            cv2.rectangle(image, (180, 180), (1220, 820), (20, 80, 20), 5)
            cv2.putText(image, "MARGIN", (120, 150), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (10, 10, 10), 2)
            path.write_bytes(cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 94])[1].tobytes())
            result = detect_paper(load_bgr(path))
            xs = [point[0] for point in result["polygon"]]
            ys = [point[1] for point in result["polygon"]]
            self.assertLess(min(xs), 150)
            self.assertGreater(max(xs), 1250)
            self.assertLess(min(ys), 150)
            self.assertGreater(max(ys), 850)

    def test_paper_rect_bridges_tears(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ragged.jpg"
            make_scan(path, rotate=1.0)
            source = load_bgr(path)
            result = detect_paper(source)
            self.assertEqual(len(result["paper_rect"]), 4)
            rect = np.asarray(result["paper_rect"], dtype=np.float32)
            inside = sum(
                1 for point in result["polygon"]
                if cv2.pointPolygonTest(rect, tuple(point), False) >= 0
            )
            self.assertGreater(inside, len(result["polygon"]) * 0.75)
            self.assertGreater(cv2.contourArea(rect), cv2.contourArea(np.asarray(result["polygon"], np.float32)))

            store = Store(Path(tmp) / "data")
            record, reason = store.import_file(path, original_name=path.name)
            self.assertIsNotNone(record, reason)
            detail = store.detect(record["id"])
            detail = store.apply_edit(record["id"], {
                "output": {"mode": "rect", "fill": "paper", "padding": 0},
            })
            rendered = render_record(source, detail)
            self.assertTrue((rendered[:, :, 3] == 255).all())
            self.assertGreaterEqual(rendered.shape[1], rect[:, 0].max() - rect[:, 0].min())

    def test_skew_large_angle(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "panel20.jpg"
            make_panel_scan(path, rotate=20.0)
            result = detect_paper(load_bgr(path))
            self.assertAlmostEqual(result["skew_angle"], 20.0, delta=1.5)

    def test_small_tilt_outline_deskew(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small_tilt.jpg"
            image = np.full((1000, 1400, 3), 18, dtype=np.uint8)
            polygon = np.array([
                [100, 110], [430, 100], [520, 145], [560, 100],
                [760, 105], [930, 165], [1300, 105],
                [1310, 890], [1020, 900], [620, 892], [270, 905], [90, 895],
            ], dtype=np.int32)
            cv2.fillPoly(image, [polygon], (234, 234, 234))
            matrix = cv2.getRotationMatrix2D((700, 500), 1.2, 1.0)
            image = cv2.warpAffine(
                image, matrix, (1400, 1000),
                borderMode=cv2.BORDER_CONSTANT, borderValue=(18, 18, 18),
            )
            path.write_bytes(cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 94])[1].tobytes())

            result = detect_paper(load_bgr(path))
            self.assertEqual(result["skew_source"], "outline")
            self.assertGreater(abs(result["skew_angle"]), 0.5)

            store = Store(Path(tmp) / "data")
            record, reason = store.import_file(path, original_name=path.name)
            self.assertIsNotNone(record, reason)
            store.detect(record["id"])
            detail = store.deskew(record["id"], force=False)
            self.assertGreater(abs(detail["fine_angle"]), 0.5)

            matrix = cv2.getRotationMatrix2D(
                (detail["width"] / 2.0, detail["height"] / 2.0),
                -float(detail["fine_angle"]), 1.0,
            )
            polygon = np.asarray(detail["polygon"], dtype=np.float32)
            rotated = (polygon @ matrix[:, :2].T + matrix[:, 2]).astype(np.float32)
            _center, _size, angle = cv2.minAreaRect(rotated.reshape(-1, 1, 2))
            if angle > 45:
                angle -= 90
            if angle <= -45:
                angle += 90
            self.assertLess(abs(angle), 0.35)

    def test_internal_hole_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hole.jpg"
            make_scan(path)
            image = load_bgr(path)
            cv2.circle(image, (700, 500), 44, (10, 10, 10), -1, lineType=cv2.LINE_AA)
            path.write_bytes(cv2.imencode(".jpg", image)[1].tobytes())
            plain = detect_paper(load_bgr(path))
            self.assertEqual(plain["holes"], [])
            holed = detect_paper(load_bgr(path), {"detect_holes": True})
            self.assertGreaterEqual(len(holed["holes"]), 1)
            mask = build_mask(load_bgr(path).shape[:2], holed["polygon"],
                              holes=holed["holes"], expand=0, feather=0)
            self.assertEqual(int(mask[500, 700]), 0)

    def test_no_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blank.jpg"
            image = np.full((500, 700, 3), 20, dtype=np.uint8)
            path.write_bytes(cv2.imencode(".jpg", image)[1].tobytes())
            with self.assertRaises(Exception):
                detect_paper(load_bgr(path))


class StoreAndExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "data")
        self.ids = []
        for index in range(2):
            path = self.root / f"page{index}.jpg"
            make_scan(path, rotate=index * 2.0)
            record, reason = self.store.import_file(path, original_name=path.name)
            self.assertIsNotNone(record, reason)
            self.ids.append(record["id"])
            self.store.detect(record["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_detect_and_thumb(self):
        detail = self.store.detail(self.ids[0])
        self.assertEqual(detail["status"], "ready")
        self.assertGreater(len(detail["polygon"]), 5)
        self.assertTrue((self.store.previews_dir / f"{self.ids[0]}_{detail['rev']}_t.png").is_file())
        self.assertTrue(self.store.ensure_preview(self.ids[0], detail["rev"], 900).is_file())

    def test_rotation_and_manual_stroke(self):
        detail = self.store.apply_edit(self.ids[0], {"rotation": 90})
        self.assertEqual(detail["rotation"], 90)
        old_w, old_h = detail["result_width"], detail["result_height"]
        self.assertGreater(old_w, 0)
        current = self.store.detail(self.ids[0])
        detail = self.store.apply_edit(self.ids[0], {
            "strokes": [{
                "mode": "add",
                "radius": 20,
                "points": [[200, 200], [220, 220]],
            }],
        })
        self.assertTrue(detail["manual"])
        self.assertEqual(len(detail["strokes"]), 1)

    def test_output_modes(self):
        record = self.store.find(self.ids[0])
        detail = self.store.detail(self.ids[0])
        source = self.store.load_source(record)

        # Default is a transparent cutout around the torn sheet.
        cutout = render_record(source, detail, max_side=700)
        self.assertEqual(cutout.shape[2], 4)
        self.assertTrue((cutout[:, :, 3] == 0).any())

        # Full rectangle filled with paper colour, plus a padding border.
        detail = self.store.apply_edit(self.ids[0], {"output": {
            "mode": "rect", "rectification": "auto", "fill": "paper",
            "padding": 20, "aspect": "paper",
        }})
        rect = render_record(source, detail, max_side=900)
        self.assertEqual(int(rect[0, 0, 3]), 255)
        self.assertGreater(rect.shape[0], 100)
        self.assertGreater(rect.shape[1], 100)
        # The padding is opaque.
        self.assertEqual(int(rect[2, 2, 3]), 255)

        # Transparent rectangle: the area outside the torn sheet is alpha 0.
        detail = self.store.apply_edit(self.ids[0], {"output": {
            "mode": "rect", "rectification": "auto", "fill": "transparent",
            "padding": 0, "aspect": "paper",
        }})
        transparent = render_record(source, detail, max_side=900)
        self.assertTrue((transparent[:, :, 3] == 0).any())
        self.assertTrue((transparent[:, :, 3] == 255).any())
        quantized = encode_bgra(transparent, "png", background=None, png_quantize=True)
        with Image.open(io.BytesIO(quantized)).convert("RGBA") as image:
            alpha = np.asarray(image.getchannel("A"))
            self.assertLess(int(alpha.min()), 255)
            self.assertEqual(int(alpha.max()), 255)

        # Original pixels mode is opaque everywhere.
        detail = self.store.apply_edit(self.ids[0], {"output": {
            "mode": "rect", "rectification": "auto", "fill": "original",
            "padding": 0, "aspect": "paper",
        }})
        original = render_record(source, detail, max_side=900)
        self.assertEqual(int(original[:, :, 3].min()), 255)

        # Four-corner perspective mode with a custom paper aspect ratio.
        quad = [[110, 60], [1270, 95], [1280, 930], [160, 950]]
        detail = self.store.apply_edit(self.ids[0], {
            "quad": quad,
            "output": {
                "mode": "rect", "rectification": "quad", "fill": "paper",
                "padding": 10, "aspect": "custom", "aspect_w": 3, "aspect_h": 2,
            },
        })
        warped = render_record(source, detail, max_side=900)
        content_ratio = (warped.shape[1] - 20) / max(1, warped.shape[0] - 20)
        self.assertAlmostEqual(content_ratio, 1.5, delta=0.08)

    def test_desaturate_output(self):
        colour = np.full((12, 12, 3), (40, 80, 200), dtype=np.uint8)
        full = apply_desaturate(colour, 100, boost=False)
        self.assertLessEqual(int(full[:, :, 0].max()) - int(full[:, :, 0].min()), 1)
        self.assertTrue(np.array_equal(full[:, :, 0], full[:, :, 1]))
        partial = apply_desaturate(colour, 50, boost=False)
        self.assertFalse(np.array_equal(partial[:, :, 0], partial[:, :, 1]))
        boosted = apply_desaturate(colour, 100, boost=True)
        self.assertTrue(np.array_equal(boosted[:, :, 1], boosted[:, :, 2]))

        detail = self.store.apply_edit(self.ids[0], {"output": {
            "mode": "cutout", "fill": "paper", "desaturate": 100,
        }})
        source = self.store.load_source(self.store.find(self.ids[0]))
        rendered = render_record(source, detail, max_side=700)
        opaque = rendered[:, :, 3] > 0
        channels = rendered[:, :, :3].astype(np.int16)
        self.assertLessEqual(int(np.abs(channels[:, :, 0] - channels[:, :, 1])[opaque].max()), 2)
        self.assertLessEqual(int(np.abs(channels[:, :, 1] - channels[:, :, 2])[opaque].max()), 2)

    def test_project_manager_create_and_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(Path(tmp) / "data")
            first = manager.projects_view()
            self.assertEqual(len(first["projects"]), 1)
            first_id = first["current"]
            first_name = first["projects"][0]["name"]

            manager.create("第二个项目")
            second = manager.projects_view()
            self.assertEqual(len(second["projects"]), 2)
            self.assertNotEqual(second["current"], first_id)
            self.assertEqual(manager.store.session["project"]["name"], "第二个项目")

            manager.open(first_id)
            self.assertEqual(manager.projects_view()["current"], first_id)
            self.assertEqual(manager.store.session["project"]["name"], first_name)


        source = np.full((80, 120, 3), (190, 195, 200), dtype=np.uint8)
        source[20:60, 30:90] = (40, 40, 190)
        source[10:14, 20:100] = (35, 35, 35)
        polygon = [[0, 0], [119, 0], [119, 79], [0, 79]]
        base = {
            "rotation": 0, "fine_angle": 0, "polygon": polygon,
            "strokes": [], "holes": [], "params": {}, "detection": {},
            "paper_color": "#c8c3be",
        }
        off = render_record(source, {
            **base, "output": {"mode": "cutout", "enhance": False},
        })
        on = render_record(source, {
            **base,
            "output": {
                "mode": "cutout", "enhance": True,
                "whiten": 85, "purity": 85,
            },
        })
        self.assertTrue(np.array_equal(off[:, :, 3], on[:, :, 3]))
        self.assertGreater(
            float(on[70, 60, :3].mean()),
            float(off[70, 60, :3].mean()) + 20,
        )
        off_patch = off[40, 60, :3].astype(np.int16)
        on_patch = on[40, 60, :3].astype(np.int16)
        self.assertGreater(
            int(on_patch.max() - on_patch.min()),
            int(off_patch.max() - off_patch.min()),
        )


        records = self.store.image_paths(self.ids)
        folder = run_export(self.store, records, "folder", {
            "path": str(self.root / "out" / "pages"),
            "format": "png",
            "background": "transparent",
            "include_manifest": True,
        })
        self.assertTrue(Path(folder["path"]).is_dir())
        self.assertTrue((Path(folder["path"]) / "manifest.csv").is_file())
        first_png = Path(folder["path"]) / "1_page0.png"
        with Image.open(first_png) as image:
            self.assertEqual(image.mode, "RGBA")
            alpha = np.asarray(image.getchannel("A"))
            self.assertEqual(int(alpha.min()), 0)
            self.assertEqual(int(alpha.max()), 255)

        pdf = run_export(self.store, records, "pdf", {
            "path": str(self.root / "out" / "pages.pdf"),
            "page": "a4",
            "dpi": 120,
            "margin_mm": 5,
        })
        self.assertTrue(Path(pdf["path"]).read_bytes().startswith(b"%PDF"))
        self.assertGreater(Path(pdf["path"]).stat().st_size, 1000)

        archive = run_export(self.store, records, "zip", {
            "path": str(self.root / "out" / "pages.zip"),
            "format": "jpg",
            "background": "white",
        })
        with zipfile.ZipFile(archive["path"]) as zf:
            names = zf.namelist()
            self.assertEqual(len([n for n in names if n.endswith(".jpg")]), 2)
            self.assertIn("manifest.csv", names)

        long_image = run_export(self.store, records, "long", {
            "path": str(self.root / "out" / "long.jpg"),
            "direction": "vertical",
            "target_size": 600,
            "gap": 10,
            "background": "white",
        })
        with Image.open(long_image["path"]) as image:
            self.assertEqual(image.width, 600)
            self.assertGreater(image.height, 400)

    def _cards_store(self, torn: bool = False) -> tuple[Store, str]:
        """A tiny project with two pieces on one scan."""
        store = Store(self.root / ("cards-data" if not torn else "torn-data"))
        image = np.full((1200, 1800, 3), 16, dtype=np.uint8)
        if torn:
            cv2.fillPoly(image, [np.array(
                [[200, 140], [880, 130], [885, 990], [205, 1000]], np.int32)], (236, 235, 232))
            cv2.fillPoly(image, [np.array(
                [[895, 150], [1570, 160], [1565, 1010], [900, 1000]], np.int32)], (230, 229, 226))
            path = self.root / "torn.jpg"
        else:
            cv2.fillPoly(image, [np.array(
                [[150, 130], [760, 110], [775, 980], [165, 1010]], np.int32)], (238, 237, 234))
            cv2.fillPoly(image, [np.array(
                [[980, 190], [1660, 210], [1650, 1080], [1000, 1060]], np.int32)], (231, 229, 226))
            path = self.root / "cards.jpg"
        Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).save(path, quality=95)
        record, reason = store.import_file(path, original_name=path.name)
        self.assertIsNotNone(record, reason)
        store.detect(record["id"])
        return store, record["id"]

    def test_detect_regions_two_cards(self):
        store, image_id = self._cards_store()
        detail = store.detail(image_id)
        self.assertEqual(len(detail["regions"]), 2)
        self.assertEqual(detail["region_count"], 2)
        self.assertEqual(detail["enabled_regions"], 2)
        self.assertEqual(detail["detect_multi"], True)
        for region in detail["regions"]:
            self.assertGreaterEqual(len(region["polygon"]), 3)
            self.assertIn("label", region)
            self.assertTrue(region["enabled"])

    def test_region_selection_rectify_and_export(self):
        store, image_id = self._cards_store()
        detail = store.detail(image_id)
        second = detail["regions"][1]["id"]
        detail = store.regions_update(image_id, second, {"rectify": True})
        self.assertEqual(detail["rectify_regions"], 1)
        detail = store.regions_update(image_id, second, {"enabled": False})
        self.assertEqual(detail["enabled_regions"], 1)
        self.assertEqual(len(store.export_records([store.find(image_id)])), 1)
        detail = store.regions_update(image_id, second, {"enabled": True})
        items = store.export_records([store.find(image_id)])
        self.assertEqual(len(items), 2)
        self.assertTrue(any(item["output"]["mode"] == "rect"
                            and item["output"]["rectification"] == "quad"
                            for item in items))
        names = {item["name"] for item in items}
        self.assertEqual(len(names), 2)
        folder = self.root / "cards-out"
        run_export(store, items, "folder", {
            "path": str(folder), "format": "png", "background": "white",
        }, lambda *args: None)
        files = sorted(path.name for path in folder.glob("*.png"))
        self.assertEqual(len(files), 2)

    def test_region_merge_requires_connection(self):
        store, image_id = self._cards_store()
        detail = store.detail(image_id)
        ids = [region["id"] for region in detail["regions"]]
        with self.assertRaises(Exception):
            store.regions_merge(image_id, ids)
        self.assertEqual(len(store.detail(image_id)["regions"]), 2)

        torn_store, torn_id = self._cards_store(torn=True)
        torn = torn_store.detail(torn_id)
        if len(torn["regions"]) >= 2:
            merged = torn_store.regions_merge(
                torn_id, [region["id"] for region in torn["regions"][:2]],
            )
            self.assertEqual(len(merged["regions"]), 1)

    def test_region_add_manual_and_delete(self):
        store, image_id = self._cards_store()
        detail = store.detail(image_id)
        before = len(detail["regions"])
        detail = store.regions_add(image_id, 420, 520)
        self.assertGreaterEqual(len(detail["regions"]), before)
        detail = store.regions_manual(
            image_id, [[60, 60], [300, 60], [300, 260], [60, 260]],
        )
        self.assertEqual(len(detail["regions"]), before + 1)
        manual = detail["regions"][-1]
        self.assertEqual(manual["source"], "manual")
        detail = store.regions_delete(image_id, manual["id"])
        self.assertEqual(len(detail["regions"]), before)

    def test_dedupe(self):
        path = self.root / "page0.jpg"
        record, reason = self.store.import_file(path, original_name="again.jpg")
        self.assertIsNone(record)
        self.assertIn("重复", reason)


if __name__ == "__main__":
    unittest.main()
