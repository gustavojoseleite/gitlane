"""The ``gitlane`` command: start the local server and open the browser.

``gitlane <path>`` opens that repo as a tab (next to the remembered tabs).
Plain ``gitlane`` opens the repo in the current folder, or, outside a repo,
the page where you pick one.
"""

from __future__ import annotations

import argparse
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__

HOST = "127.0.0.1"  # never 0.0.0.0 (SPEC.md §3)
MIN_GIT = (2, 23)  # first version with `git switch` and `git restore`


def check_git() -> str | None:
    """Return a readable problem if Git is missing or too old, else None.

    Runs before GitPython is imported: GitPython fails at import time with
    a cryptic message when there is no ``git`` program.
    """
    exe = shutil.which("git")
    if exe is None:
        return ("Git is not installed (or not on your PATH). Install it from "
                "https://git-scm.com/downloads and try again.")
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return f"Could not run {exe}. Check that Git works in a terminal: git --version"
    m = re.search(r"(\d+)\.(\d+)", out)
    if m and (int(m[1]), int(m[2])) < MIN_GIT:
        need = ".".join(map(str, MIN_GIT))
        return f"Your Git is too old ({out.strip()}). gitlane needs Git {need} or newer."
    return None


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="gitlane", description="Open a Git repository in your browser.")
    p.add_argument("path", nargs="?", default=None,
                   help="folder inside a repository (default: the current folder, if it is one)")
    p.add_argument("--port", type=int, default=None, help="port to listen on (default: a free port)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    p.add_argument("--install-launcher", action="store_true",
                   help="add gitlane to your app menu (Linux), then exit")
    p.add_argument("--remove-launcher", action="store_true",
                   help="remove gitlane from your app menu (Linux), then exit")
    p.add_argument("--version", action="version", version=f"gitlane {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.install_launcher or args.remove_launcher:
        from . import launcher

        try:
            msg = launcher.install() if args.install_launcher else launcher.remove()
        except launcher.LauncherError as e:
            print(f"gitlane: {e}", file=sys.stderr)
            return 1
        print(msg)
        return 0

    problem = check_git()
    if problem:
        print(f"gitlane: {problem}", file=sys.stderr)
        return 1

    from .git_ops import NotAGitRepo, open_repo

    try:
        repo = open_repo(args.path or ".")
    except NotAGitRepo as e:
        if args.path is not None:  # asked for a specific folder: say what's wrong
            print(f"gitlane: {e}", file=sys.stderr)
            print("Pass a folder inside a Git repository, e.g. gitlane ~/dev/myproject, "
                  "or run plain gitlane and pick one in the browser.", file=sys.stderr)
            return 1
        repo_root = None  # pick a repo in the browser
    else:
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
        print(f"gitlane {__version__} — {repo_root or 'pick a repository in the browser'}", flush=True)
        print(f"Open: {url}", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)

    threading.Thread(target=announce, daemon=True).start()
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
