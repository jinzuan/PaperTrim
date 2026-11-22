"""HTTP-level smoke tests: token gate, upload, job polling and export."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
import zipfile
from pathlib import Path

import cv2
import httpx
import numpy as np

from app.jobs import JobManager
from app.server import create_app
from app.store import Store


def make_scan(path: Path) -> None:
    image = np.full((900, 1200, 3), 16, dtype=np.uint8)
    polygon = np.array([
        [100, 70], [520, 55], [660, 85], [1100, 65], [1130, 300],
        [1090, 560], [1120, 840], [760, 855], [670, 820], [420, 860],
        [140, 840], [110, 560], [130, 280],
    ], dtype=np.int32)
    cv2.fillPoly(image, [polygon], (230, 230, 226), lineType=cv2.LINE_AA)
    cv2.putText(image, "DEMO", (320, 420), cv2.FONT_HERSHEY_SIMPLEX,
                2.2, (25, 25, 25), 4, cv2.LINE_AA)
    path.write_bytes(cv2.imencode(".jpg", image)[1].tobytes())


class ApiTests(unittest.TestCase):
    def test_token_upload_export(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = Store(root / "data")
                jobs = JobManager()
                token = "test-token-123"
                app = create_app(store, jobs, token=token)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://testserver") as client:
                    denied = await client.get("/api/session")
                    self.assertEqual(denied.status_code, 403)

                    root_page = await client.get("/")
                    self.assertEqual(root_page.status_code, 200)
                    self.assertIn(token, root_page.text)

                    bad_host = await client.get("/", headers={"host": "evil.example"})
                    self.assertEqual(bad_host.status_code, 403)

                    scan = root / "demo scan.jpg"
                    make_scan(scan)
                    upload = await client.post(
                        "/api/import",
                        headers={"X-PaperTrim-Token": token},
                        files={"files": (scan.name, scan.read_bytes(), "image/jpeg")},
                        data={"dedupe": "true"},
                    )
                    self.assertEqual(upload.status_code, 200, upload.text)
                    payload = upload.json()
                    self.assertEqual(len(payload["imported"]), 1)
                    job = payload["job"]
                    session = None
                    for _ in range(60):
                        await asyncio.sleep(0.15)
                        job_response = await client.get(
                            f"/api/jobs/{job['id']}",
                            headers={"X-PaperTrim-Token": token},
                        )
                        job = job_response.json()
                        if job["status"] != "running":
                            break
                    self.assertEqual(job["status"], "done", job)
                    session_response = await client.get(
                        "/api/session", headers={"X-PaperTrim-Token": token}
                    )
                    session = session_response.json()
                    self.assertEqual(session["counts"]["ready"], 1)
                    image_id = session["images"][0]["id"]

                    rotate = await client.patch(
                        f"/api/images/{image_id}",
                        headers={"X-PaperTrim-Token": token},
                        json={"rotation": 90},
                    )
                    self.assertEqual(rotate.status_code, 200, rotate.text)
                    self.assertEqual(rotate.json()["rotation"], 90)

                    batch = await client.post(
                        "/api/batch",
                        headers={"X-PaperTrim-Token": token},
                        json={
                            "ids": [image_id],
                            "action": "output",
                            "output": {
                                "mode": "rect", "rectification": "auto",
                                "fill": "paper", "padding": 12, "aspect": "paper",
                            },
                            "edge": -1,
                            "align": "all",
                            "remember": True,
                        },
                    )
                    self.assertEqual(batch.status_code, 200, batch.text)
                    batch_job = batch.json()["job"]
                    for _ in range(60):
                        await asyncio.sleep(0.15)
                        batch_state = (await client.get(
                            f"/api/jobs/{batch_job['id']}",
                            headers={"X-PaperTrim-Token": token},
                        )).json()
                        if batch_state["status"] != "running":
                            break
                    self.assertEqual(batch_state["status"], "done", batch_state)
                    session = (await client.get(
                        "/api/session", headers={"X-PaperTrim-Token": token}
                    )).json()
                    record = next(item for item in session["images"] if item["id"] == image_id)
                    self.assertEqual(record["output"]["mode"], "rect")
                    self.assertEqual(session["defaults"]["output"]["mode"], "rect")
                    self.assertEqual(session["defaults"]["align_edge"], "all")
                    self.assertEqual(record["align_edge"], "all")

                    aligned = await client.post(
                        f"/api/images/{image_id}/align",
                        headers={"X-PaperTrim-Token": token},
                        json={"align": "top"},
                    )
                    self.assertEqual(aligned.status_code, 200, aligned.text)
                    self.assertEqual(aligned.json()["align_edge"], "top")

                    preview = await client.post(
                        f"/api/images/{image_id}/render-preview",
                        headers={"X-PaperTrim-Token": token},
                        json={"output": {"mode": "rect", "fill": "paper", "enhance": True}},
                    )
                    self.assertEqual(preview.status_code, 200, preview.text)
                    self.assertTrue(preview.headers["content-type"].startswith("image/png"))
                    self.assertGreater(len(preview.content), 1000)

                    overlay = await client.post(
                        f"/api/images/{image_id}/render-preview",
                        headers={"X-PaperTrim-Token": token},
                        json={"output": {"mode": "cutout", "desaturate": 100}, "overlay": True},
                    )
                    self.assertEqual(overlay.status_code, 200, overlay.text)
                    self.assertGreater(len(overlay.content), 1000)

                    detail = (await client.get(
                        f"/api/images/{image_id}", headers={"X-PaperTrim-Token": token}
                    )).json()
                    self.assertTrue(detail["regions"])
                    region_id = detail["regions"][0]["id"]
                    marked = await client.post(
                        f"/api/images/{image_id}/regions",
                        headers={"X-PaperTrim-Token": token},
                        json={"action": "update", "id": region_id, "patch": {"rectify": True}},
                    )
                    self.assertEqual(marked.status_code, 200, marked.text)
                    self.assertEqual(marked.json()["rectify_regions"], 1)
                    region_preview = await client.post(
                        f"/api/images/{image_id}/render-preview",
                        headers={"X-PaperTrim-Token": token},
                        json={"output": {"mode": "cutout"}, "region_id": region_id},
                    )
                    self.assertEqual(region_preview.status_code, 200, region_preview.text)
                    self.assertGreater(len(region_preview.content), 500)

                    saved = await client.post(
                        "/api/project/save",
                        headers={"X-PaperTrim-Token": token},
                    )
                    self.assertEqual(saved.status_code, 200, saved.text)
                    self.assertTrue(saved.json()["ok"])

                    export = await client.post(
                        "/api/export",
                        headers={"X-PaperTrim-Token": token},
                        json={
                            "kind": "zip",
                            "all": True,
                            "options": {
                                "path": str(root / "out" / "pages.zip"),
                                "format": "jpg",
                                "background": "white",
                            },
                        },
                    )
                    self.assertEqual(export.status_code, 200, export.text)
                    export_job_id = export.json()["job"]["id"]
                    export_job = None
                    for _ in range(80):
                        await asyncio.sleep(0.15)
                        export_job = (await client.get(
                            f"/api/jobs/{export_job_id}",
                            headers={"X-PaperTrim-Token": token},
                        )).json()
                        if export_job["status"] != "running":
                            break
                    self.assertEqual(export_job["status"], "done", export_job)
                    archive = Path(export_job["result"]["path"])
                    self.assertTrue(archive.is_file())
                    with zipfile.ZipFile(archive) as zf:
                        self.assertTrue(any(name.endswith(".jpg") for name in zf.namelist()))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
