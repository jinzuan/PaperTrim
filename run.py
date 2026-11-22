"""PaperTrim launcher: start the local server and open the browser."""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from app.jobs import JobManager
from app.projects import ProjectManager
from app.server import create_app
from app.util import APP_NAME, APP_VERSION


DEFAULT_PORT = 8765


def _free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _default_data_dir() -> Path:
    override = os.environ.get("PAPERTRIM_DATA_DIR")
    if override:
        return Path(override).expanduser()
    # Keep project files next to the application when possible, so the whole
    # working folder can be copied around.  Fall back to LOCALAPPDATA if the
    # application directory is read-only (e.g. installed under Program Files).
    root = _app_root()
    candidate = root / "workspace"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return candidate
    except OSError:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = Path(base) / "PaperTrim"
        path.mkdir(parents=True, exist_ok=True)
        return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} — 扫描裁切与打包工具")
    parser.add_argument("--data-dir", type=str, default=None,
                        help="数据目录（默认为程序目录下的 workspace）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}，被占用时自动换一个）")
    parser.add_argument("--no-browser", action="store_true",
                        help="只启动服务，不自动打开浏览器")
    parser.add_argument("--version", action="store_true", help="显示版本号后退出")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0

    data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else _default_data_dir()
    projects = ProjectManager(data_dir)
    jobs = JobManager()
    port = _free_port(max(1, min(65535, int(args.port))))
    app = create_app(projects, jobs, port=port)
    url = f"http://127.0.0.1:{port}/"

    print("=" * 62)
    print(f"  {APP_NAME} {APP_VERSION}  ·  扫描裁切与打包工具")
    print("=" * 62)
    print(f"  本地地址 : {url}")
    print(f"  数据目录 : {data_dir}")
    print("  隐私说明 : 所有图片只在本机处理，不上传、不联网。")
    print("  关闭程序 : 在此窗口按 Ctrl+C")
    print("=" * 62)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    except KeyboardInterrupt:
        print("\n已退出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
