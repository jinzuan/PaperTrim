# Third-party notices

PaperTrim is MIT licensed (see `LICENSE`). It uses the following open-source
components at runtime. Their licenses apply to those components.

| Component | License | Project |
|---|---|---|
| Starlette | BSD-3-Clause | https://www.starlette.io/ |
| Uvicorn | BSD-3-Clause | https://www.uvicorn.org/ |
| OpenCV | Apache-2.0 | https://opencv.org/ |
| NumPy | BSD-3-Clause | https://numpy.org/ |
| Pillow | HPND | https://python-pillow.org/ |
| python-multipart | Apache-2.0 | https://github.com/Kludex/python-multipart |

The Windows build produced by `tools/build_exe.ps1` bundles these libraries
through PyInstaller. The full license texts are available at the project URLs
above and in the installed Python packages under their respective
`LICENSE`/`COPYING` files.

No third-party service, telemetry endpoint, CDN, or remote font is used at
runtime.
