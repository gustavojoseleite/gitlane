"""FastAPI app and security middleware (SPEC.md §3)."""

from __future__ import annotations

import secrets
from dataclasses import asdict
from html import escape
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .git_ops import FileChange, RepoStatus, get_status, open_repo

COOKIE_NAME = "gitlane_token"
STATIC_DIR = Path(str(resources.files("gitlane") / "static"))


def create_app(repo_path: str | Path, token: str, port: int) -> FastAPI:
    """Build the app for one repository.

    ``token`` is the secret from the startup URL; ``port`` is used for the
    Host-header check (DNS-rebinding protection).
    """
    repo_path = Path(repo_path)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    app = FastAPI(title="gitlane", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return PlainTextResponse("Forbidden: invalid Host header.", status_code=403)

        query_token = request.query_params.get("token")
        if query_token is not None:
            if request.method == "GET" and secrets.compare_digest(query_token, token):
                # Trade the URL token for a cookie, then drop it from the address bar.
                response = RedirectResponse(request.url.path, status_code=303)
                response.set_cookie(
                    COOKIE_NAME, token, httponly=True, samesite="strict", path="/",
                )
                return response
            return _forbidden()

        cookie = request.cookies.get(COOKIE_NAME, "")
        if not secrets.compare_digest(cookie, token):
            return _forbidden()
        return await call_next(request)

    def load_status() -> RepoStatus:
        repo = open_repo(repo_path)
        try:
            return get_status(repo)
        finally:
            repo.close()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        status = load_status()
        data = asdict(status)
        data["detached"] = status.detached
        data["clean"] = status.clean
        return JSONResponse(data)

    @app.get("/partials/status", response_class=HTMLResponse)
    def partial_status() -> str:
        return render_status(load_status())

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _forbidden() -> PlainTextResponse:
    return PlainTextResponse(
        "Forbidden: open gitlane using the link printed in your terminal.",
        status_code=403,
    )


# --- HTML fragments -------------------------------------------------------

def render_status(s: RepoStatus) -> str:
    if s.branch:
        head = f'<span class="branch">{escape(s.branch)}</span>'
    elif s.detached:
        head = f'<span class="branch detached">detached at {escape(s.head_commit[:7])}</span>'
    else:
        head = '<span class="branch">no branch</span>'

    if s.head_commit:
        commit = (
            f'<code>{escape(s.head_commit[:7])}</code> {escape(s.head_summary or "")}'
        )
    else:
        commit = '<span class="muted">No commits yet</span>'

    if s.upstream:
        sync = f'tracking <code>{escape(s.upstream)}</code> · ↑{s.ahead} ↓{s.behind}'
    elif s.branch:
        sync = '<span class="muted">no upstream</span>'
    else:
        sync = ""

    parts = [
        '<section class="repo">',
        f'<h1>{escape(s.name)}</h1>',
        f'<p class="path">{escape(s.root)}</p>',
        f'<p>{head} {commit}</p>',
        f'<p class="sync">{sync}</p>' if sync else "",
        "</section>",
    ]
    if s.clean:
        parts.append('<p class="clean">Working tree clean ✓</p>')
    else:
        parts += [
            _file_list("Conflicts", s.conflicted, "conflict"),
            _file_list("Staged", s.staged, "staged"),
            _file_list("Changes", s.unstaged, "unstaged"),
            _file_list("Untracked", s.untracked, "untracked"),
        ]
    return "\n".join(p for p in parts if p)


def _file_list(title: str, files: list[FileChange], css: str) -> str:
    if not files:
        return ""
    items = []
    for f in files:
        name = escape(f.path)
        if f.orig_path:
            name = f"{escape(f.orig_path)} → {name}"
        items.append(f'<li><span class="tag {escape(f.status.replace(" ", "-"))}">{escape(f.status)}</span> {name}</li>')
    return (
        f'<section class="files {css}"><h2>{title} <span class="count">{len(files)}</span></h2>'
        f'<ul>{"".join(items)}</ul></section>'
    )
