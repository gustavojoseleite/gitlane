"""The ``gitlane`` command: find the repo, start the local server, open the browser."""

from __future__ import annotations

import argparse
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__
from .git_ops import NotAGitRepo, open_repo

HOST = "127.0.0.1"  # never 0.0.0.0 (SPEC.md §3)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="gitlane", description="Open a Git repository in your browser.")
    p.add_argument("path", nargs="?", default=".", help="folder inside the repository (default: current folder)")
    p.add_argument("--port", type=int, default=None, help="port to listen on (default: a free port)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    p.add_argument("--version", action="version", version=f"gitlane {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        repo = open_repo(args.path)
    except NotAGitRepo as e:
        print(f"gitlane: {e}", file=sys.stderr)
        print("Run gitlane inside a Git repository, or pass its path: gitlane ~/dev/myproject", file=sys.stderr)
        return 1
    repo_root = Path(repo.working_tree_dir)
    repo.close()

    import uvicorn  # imported late so `gitlane --help` stays fast

    from .server import create_app

    port = args.port or find_free_port()
    token = secrets.token_urlsafe(32)
    url = f"http://{HOST}:{port}/?token={token}"

    app = create_app(repo_root, token, port)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))

    def announce() -> None:
        while not server.started and not server.should_exit:
            time.sleep(0.05)
        if server.should_exit:
            return
        print(f"gitlane {__version__} — {repo_root}", flush=True)
        print(f"Open: {url}", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)

    threading.Thread(target=announce, daemon=True).start()
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
