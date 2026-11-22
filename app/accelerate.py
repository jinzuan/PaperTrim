"""Small performance bootstrap.

OpenCV already uses multiple CPU cores for many operations.  We make the
choice explicit, honour PAPERTRIM_THREADS/PAPERTRIM_OPENCL, and only enable
OpenCL when the local build actually has a device.  No image data leaves the
machine either way.
"""
from __future__ import annotations

import os
from typing import Any

import cv2


def configure() -> dict[str, Any]:
    cpu = max(1, int(os.cpu_count() or 1))
    raw_threads = os.environ.get("PAPERTRIM_THREADS", "").strip()
    if raw_threads:
        try:
            threads = max(1, min(cpu, int(raw_threads)))
        except ValueError:
            threads = max(1, cpu - 1)
    else:
        threads = max(1, cpu - 1)
    try:
        cv2.setUseOptimized(True)
        cv2.setNumThreads(threads)
    except Exception:
        pass

    opencl = False
    if os.environ.get("PAPERTRIM_OPENCL", "1").strip() not in {"0", "false", "off"}:
        try:
            if cv2.ocl.haveOpenCL():
                cv2.ocl.setUseOpenCL(True)
                opencl = bool(cv2.ocl.useOpenCL())
        except Exception:
            opencl = False
    return {"cpu_count": cpu, "threads": threads, "opencl": opencl}
