"""FastAPI app and security middleware (SPEC.md §3)."""

from __future__ import annotations

import json
import re
import secrets
from contextvars import ContextVar
from dataclasses import asdict
from html import escape
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, blobs, branches, git_ops, graph, stash, tags, workspace
from .git_ops import FileChange, FileDiff, GitError, NotAGitRepo, RepoStatus, get_status, open_repo
from .workspace import Workspace

COOKIE_NAME = "gitlane_token"
STATIC_DIR = Path(str(resources.files("gitlane") / "static"))
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
REPO_HEADER = "X-Gitlane-Repo"

# Id of the tab a request is for: the X-Gitlane-Repo header, or ?repo= for
# images (an <img> can't send headers). Set by the middleware.
_current_repo: ContextVar[str | None] = ContextVar("gitlane_repo", default=None)


class NoRepo(Exception):
    """The request names no open tab (closed meanwhile, or none chosen)."""


def create_app(repo_path: str | Path | None, token: str, port: int,
               ws: Workspace | None = None) -> FastAPI:
    """Build the app.

    ``repo_path`` (optional) is opened as a tab next to the remembered ones;
    ``token`` is the secret from the startup URL; ``port`` is used for the
    Host-header check (DNS-rebinding protection).
    """
    ws = ws or Workspace()
    if repo_path is not None:
        ws.open(repo_path)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    app = FastAPI(title="gitlane", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return PlainTextResponse("Forbidden: invalid Host header.", status_code=403)

        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return PlainTextResponse("Forbidden: cross-origin request.", status_code=403)

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
        _current_repo.set(request.headers.get(REPO_HEADER) or request.query_params.get("repo"))
        return await call_next(request)

    @app.exception_handler(NoRepo)
    async def no_repo(request: Request, exc: NoRepo):
        return PlainTextResponse("This repo isn't open in gitlane any more. Reload the page.",
                                 status_code=409)

    def with_repo(fn, *args):
        path = ws.path_of(_current_repo.get())
        if path is None:
            raise NoRepo()
        repo = open_repo(path)
        try:
            return fn(repo, *args)
        finally:
            repo.close()

    def status_response(error: str | None = None, notice: str | None = None,
                        events: tuple[str, ...] = (), hint: str | None = None,
                        action: dict | None = None) -> HTMLResponse:
        def build(repo):
            return render_status(get_status(repo), error=error, notice=notice, hint=hint,
                                 action=action, branch_lists=branches.list_branches(repo),
                                 stashes=stash.list_stashes(repo), tag_list=tags.list_tags(repo))
        html = with_repo(build)
        # After the swap: a handler may clear the panel holding the clicked button, and
        # htmx needs that button in the page to place the out-of-band toolbar.
        headers = {"HX-Trigger-After-Swap": ", ".join(events)} if events else {}
        return HTMLResponse(html, headers=headers)

    async def run_action(fn, *args, notice: str | None = None,
                         extra_events: tuple[str, ...] = ()) -> HTMLResponse:
        try:
            result = with_repo(fn, *args)
        except (GitError, NotAGitRepo) as e:
            return status_response(error=str(e), hint=getattr(e, "hint", None),
                                   action=getattr(e, "action", None), events=("gitlane:changed",))
        if callable(notice):
            notice = notice(result)
        return status_response(notice=notice, events=("gitlane:changed", *extra_events))

    # --- pages & reads -----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{VERSION}}", escape(__version__))

    # --- tabs & repo picker (milestone 7) -------------------------------------

    @app.get("/api/workspace")
    def api_workspace() -> JSONResponse:
        return JSONResponse(ws.as_dict())

    @app.post("/api/workspace/open")
    async def api_open(request: Request) -> JSONResponse:
        try:
            tab = ws.open((await _form(request)).get("path", [""])[0])
        except NotAGitRepo as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"tab": tab.as_dict(), **ws.as_dict()})

    @app.post("/api/workspace/close")
    async def api_close(request: Request) -> JSONResponse:
        ws.close((await _form(request)).get("id", [""])[0])
        return JSONResponse(ws.as_dict())

    @app.post("/api/workspace/activate")
    async def api_activate(request: Request) -> JSONResponse:
        ws.activate((await _form(request)).get("id", [""])[0])
        return JSONResponse(ws.as_dict())

    @app.get("/partials/picker", response_class=HTMLResponse)
    def partial_picker(path: str | None = None) -> str:
        try:
            listing, error = workspace.browse(path), None
        except NotAGitRepo as e:
            listing, error = workspace.browse(None), str(e)
        return render_picker(ws.recent_repos(), listing, error)

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        status = with_repo(get_status)
        data = asdict(status)
        data["detached"] = status.detached
        data["clean"] = status.clean
        return JSONResponse(data)

    @app.get("/partials/status", response_class=HTMLResponse)
    def partial_status() -> HTMLResponse:
        return status_response()

    @app.get("/partials/diff", response_class=HTMLResponse)
    def partial_diff(path: str, staged: int = 0) -> str:
        try:
            diff = with_repo(git_ops.diff_file, path, bool(staged))
            images = None
            if blobs.image_type(path):
                before, after = (blobs.HEAD, blobs.INDEX) if staged else (blobs.INDEX, blobs.WORKTREE)
                images = with_repo(image_pair, (before, path), (after, path))
        except GitError as e:
            return f'<p class="error">{escape(str(e))}</p>'
        return render_diff(diff, images=images)

    # --- actions (POST only) ----------------------------------------------

    @app.post("/actions/stage", response_class=HTMLResponse)
    async def action_stage(request: Request) -> HTMLResponse:
        return await run_action(git_ops.stage, await _paths(request))

    @app.post("/actions/stage-all", response_class=HTMLResponse)
    async def action_stage_all() -> HTMLResponse:
        return await run_action(git_ops.stage_all)

    @app.post("/actions/unstage", response_class=HTMLResponse)
    async def action_unstage(request: Request) -> HTMLResponse:
        return await run_action(git_ops.unstage, await _paths(request))

    @app.post("/actions/unstage-all", response_class=HTMLResponse)
    async def action_unstage_all() -> HTMLResponse:
        return await run_action(git_ops.unstage_all)

    @app.post("/actions/discard", response_class=HTMLResponse)
    async def action_discard(request: Request) -> HTMLResponse:
        paths = await _paths(request)
        if len(paths) != 1:
            return status_response(error="Discard works on one file at a time.")
        return await run_action(git_ops.discard, paths[0],
                                notice=f"Discarded changes to {paths[0]}.")

    @app.post("/actions/commit", response_class=HTMLResponse)
    async def action_commit(request: Request) -> HTMLResponse:
        form = await _form(request)
        message = form.get("message", [""])[0]
        if "summary" in form:  # two-field form: gitlane adds the blank line Git needs
            summary = form["summary"][0].strip()
            description = form.get("description", [""])[0].strip()
            message = summary if not summary or not description else f"{summary}\n\n{description}"
        return await run_action(git_ops.commit, message,
                                notice=lambda sha: f"Committed {sha}.",
                                extra_events=("gitlane:committed",))

    @app.get("/partials/graph", response_class=HTMLResponse)
    def partial_graph(skip: int = 0) -> str:
        return render_graph_page(with_repo(graph.graph_page, skip))

    @app.get("/partials/commit", response_class=HTMLResponse)
    def partial_commit(sha: str) -> str:
        try:
            return render_commit(with_repo(graph.commit_detail, sha))
        except GitError as e:
            return f'<p class="error">{escape(str(e))}</p>'

    @app.get("/partials/commit-diff", response_class=HTMLResponse)
    def partial_commit_diff(sha: str, path: str, orig: str | None = None) -> str:
        try:
            diff = with_repo(graph.commit_file_diff, sha, path, orig)
            images = None
            if blobs.image_type(path):
                images = with_repo(commit_image_pair, sha, path, orig)
        except GitError as e:
            return f'<p class="error">{escape(str(e))}</p>'
        return render_diff(diff, where=f"commit {sha[:7]}", images=images)

    def image_pair(repo, before, after):
        return tuple(blob_url(rev, p) if blobs.blob_exists(repo, rev, p) else None
                     for rev, p in (before, after))

    def commit_image_pair(repo, sha, path, orig):
        sha = graph.resolve_commit(repo, sha)
        parents = repo.commit(sha).parents
        before = (parents[0].hexsha, orig or path) if parents else None
        after = blob_url(sha, path) if blobs.blob_exists(repo, sha, path) else None
        before_url = (blob_url(*before) if before and blobs.blob_exists(repo, *before) else None)
        return before_url, after

    @app.get("/blob")
    def blob(rev: str, path: str) -> Response:
        mime = blobs.image_type(path)
        if mime is None:
            return PlainTextResponse("Only images can be previewed.", status_code=415)
        try:
            data = with_repo(blobs.read_blob, rev, path)
        except GitError as e:
            return PlainTextResponse(str(e), status_code=400)
        if data is None:
            return PlainTextResponse("File not found at this version.", status_code=404)
        immutable = rev not in (blobs.WORKTREE, blobs.INDEX, blobs.HEAD)
        return Response(data, media_type=mime, headers={
            # An SVG opened directly must never run scripts in gitlane's origin.
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=86400" if immutable else "no-store",
        })

    @app.get("/api/refs-version")
    def api_refs_version() -> JSONResponse:
        return JSONResponse({"version": with_repo(graph.refs_version)})

    @app.post("/actions/fetch", response_class=HTMLResponse)
    async def action_fetch() -> HTMLResponse:
        return await run_action(git_ops.fetch, notice=lambda msg: msg)

    @app.post("/actions/pull", response_class=HTMLResponse)
    async def action_pull() -> HTMLResponse:
        return await run_action(git_ops.pull, notice=lambda msg: msg)

    @app.post("/actions/push", response_class=HTMLResponse)
    async def action_push() -> HTMLResponse:
        return await run_action(git_ops.push, notice=lambda msg: msg)

    @app.post("/actions/force-push", response_class=HTMLResponse)
    async def action_force_push() -> HTMLResponse:
        return await run_action(git_ops.push, True, notice=lambda msg: msg)

    # --- branches & commit operations (milestone 5) ---------------------------

    async def field(request: Request, name: str) -> str:
        return (await _form(request)).get(name, [""])[0]

    @app.post("/actions/branch/create", response_class=HTMLResponse)
    async def action_branch_create(request: Request) -> HTMLResponse:
        form = await _form(request)
        name = form.get("name", [""])[0]
        start = form.get("start", [""])[0] or None
        return await run_action(branches.create_branch, name, start, notice=lambda m: m)

    @app.post("/actions/branch/checkout", response_class=HTMLResponse)
    async def action_branch_checkout(request: Request) -> HTMLResponse:
        return await run_action(branches.checkout, await field(request, "name"), notice=lambda m: m)

    @app.post("/actions/branch/merge", response_class=HTMLResponse)
    async def action_branch_merge(request: Request) -> HTMLResponse:
        return await run_action(branches.merge, await field(request, "name"), notice=lambda m: m)

    @app.post("/actions/branch/delete", response_class=HTMLResponse)
    async def action_branch_delete(request: Request) -> HTMLResponse:
        form = await _form(request)
        return await run_action(branches.delete_branch, form.get("name", [""])[0],
                                form.get("force", [""])[0] == "1", notice=lambda m: m)

    @app.post("/actions/branch/delete-remote", response_class=HTMLResponse)
    async def action_branch_delete_remote(request: Request) -> HTMLResponse:
        return await run_action(branches.delete_remote_branch, await field(request, "name"),
                                notice=lambda m: m)

    # --- tags ------------------------------------------------------------------

    @app.post("/actions/tag/create", response_class=HTMLResponse)
    async def action_tag_create(request: Request) -> HTMLResponse:
        form = await _form(request)
        return await run_action(tags.create_tag, form.get("name", [""])[0], form.get("sha", [""])[0],
                                form.get("message", [""])[0], notice=lambda m: m)

    @app.post("/actions/tag/delete", response_class=HTMLResponse)
    async def action_tag_delete(request: Request) -> HTMLResponse:
        return await run_action(tags.delete_tag, await field(request, "name"), notice=lambda m: m)

    @app.post("/actions/tag/push", response_class=HTMLResponse)
    async def action_tag_push(request: Request) -> HTMLResponse:
        return await run_action(tags.push_tag, await field(request, "name"), notice=lambda m: m)

    @app.post("/actions/tag/delete-remote", response_class=HTMLResponse)
    async def action_tag_delete_remote(request: Request) -> HTMLResponse:
        return await run_action(tags.delete_remote_tag, await field(request, "name"), notice=lambda m: m)

    @app.post("/actions/commit/checkout", response_class=HTMLResponse)
    async def action_commit_checkout(request: Request) -> HTMLResponse:
        return await run_action(branches.checkout_commit, await field(request, "sha"), notice=lambda m: m)

    @app.post("/actions/commit/cherry-pick", response_class=HTMLResponse)
    async def action_cherry_pick(request: Request) -> HTMLResponse:
        return await run_action(branches.cherry_pick, await field(request, "sha"), notice=lambda m: m)

    @app.post("/actions/commit/revert", response_class=HTMLResponse)
    async def action_revert(request: Request) -> HTMLResponse:
        return await run_action(branches.revert, await field(request, "sha"), notice=lambda m: m)

    @app.post("/actions/commit/reset", response_class=HTMLResponse)
    async def action_reset(request: Request) -> HTMLResponse:
        form = await _form(request)
        return await run_action(branches.reset, form.get("sha", [""])[0], form.get("mode", [""])[0],
                                notice=lambda m: m)

    @app.post("/actions/operation/abort", response_class=HTMLResponse)
    async def action_abort() -> HTMLResponse:
        return await run_action(branches.abort_operation, notice=lambda m: m)

    @app.post("/actions/operation/continue", response_class=HTMLResponse)
    async def action_continue() -> HTMLResponse:
        return await run_action(branches.continue_operation, notice=lambda m: m)

    # --- stash (milestone 6) --------------------------------------------------

    @app.post("/actions/stash/save", response_class=HTMLResponse)
    async def action_stash_save(request: Request) -> HTMLResponse:
        return await run_action(stash.save, await field(request, "message"), notice=lambda m: m)

    def stash_action(fn):
        async def handler(request: Request) -> HTMLResponse:
            form = await _form(request)
            return await run_action(fn, form.get("index", [""])[0], form.get("sha", [""])[0],
                                    notice=lambda m: m)
        return handler

    app.post("/actions/stash/apply", response_class=HTMLResponse)(stash_action(stash.apply))
    app.post("/actions/stash/pop", response_class=HTMLResponse)(stash_action(stash.pop))
    app.post("/actions/stash/drop", response_class=HTMLResponse)(stash_action(stash.drop))

    @app.get("/partials/stash", response_class=HTMLResponse)
    def partial_stash(index: int, sha: str) -> str:
        try:
            return render_stash(with_repo(stash.detail, index, sha))
        except GitError as e:
            return f'<p class="error">{escape(str(e))}</p>'

    @app.get("/partials/stash-diff", response_class=HTMLResponse)
    def partial_stash_diff(index: int, sha: str, path: str) -> str:
        try:
            diff = with_repo(stash.file_diff, index, sha, path)
            images = None
            if blobs.image_type(path):
                before, after = with_repo(stash.versions, sha, path)
                images = (blob_url(before, path) if before else None,
                          blob_url(after, path) if after else None)
        except GitError as e:
            return f'<p class="error">{escape(str(e))}</p>'
        return render_diff(diff, where=f"stash@{{{index}}}", images=images)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


async def _form(request: Request) -> dict[str, list[str]]:
    """Parse an urlencoded form body (no python-multipart dependency)."""
    body = (await request.body()).decode("utf-8", errors="replace")
    return parse_qs(body, keep_blank_values=True)


async def _paths(request: Request) -> list[str]:
    form = await _form(request)
    return [p for key in ("path", "orig") for p in form.get(key, []) if p]


def _forbidden() -> PlainTextResponse:
    return PlainTextResponse(
        "Forbidden: open gitlane using the link printed in your terminal.",
        status_code=403,
    )


# --- HTML fragments -------------------------------------------------------

def _wrap(name: str) -> str:
    """Escape a path or branch name and allow line breaks only after '/' and '_'."""
    return escape(name).replace("/", "/<wbr>").replace("_", "_<wbr>")


def _attr(value) -> str:
    """Escape a value for use inside a double-quoted HTML attribute."""
    return escape(value if isinstance(value, str) else json.dumps(value), quote=True)


_ICONS = {  # 24x24 stroke icons (drawn with currentColor)
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "trash": '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
    "merge": '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/>',
    "fetch": '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    "pull": '<path d="M12 4v13"/><path d="m6 11 6 6 6-6"/>',
    "push": '<path d="M12 20V7"/><path d="m6 13 6-6 6 6"/>',
    "force": '<path d="M12 20V7"/><path d="m6 13 6-6 6 6"/><path d="M6 3h12"/>',
    "branch": '<circle cx="6" cy="5" r="2.5"/><circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="7" r="2.5"/>'
              '<path d="M6 7.5v9"/><path d="M18 9.5a6 6 0 0 1-6 6H8"/>',
    "stash": '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9"/>'
             '<path d="M10 13h4"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "repo": '<path d="M5 4a1 1 0 0 1 1-1h13v15H6a1 1 0 0 0-1 1z"/><path d="M5 19a2 2 0 0 0 2 2h12v-3"/>'
            '<path d="M9 7h6"/>',
    "up": '<path d="m6 15 6-6 6 6"/>',
    "chevron": '<path d="m9 6 6 6-6 6"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "tag": '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/>'
           '<circle cx="7.5" cy="7.5" r="1.5"/>',
    "local": '<rect x="4" y="5" width="16" height="11" rx="1.5"/><path d="M2 19h20"/>',
    "cloud": '<path d="M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 8.5a4.5 4.5 0 0 1-.5 9.5z"/>',
    "cloud-off": '<path d="M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 8.5a4.5 4.5 0 0 1-.5 9.5z"/>'
                 '<path d="M3 3l18 18"/>',
}


def _icon(name: str, size: int = 14) -> str:
    return (f'<svg class="icon" viewBox="0 0 24 24" width="{size}" height="{size}" aria-hidden="true" '
            f'fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
            f'stroke-linejoin="round">{_ICONS[name]}</svg>')


def _post(url: str, vals: dict | None = None, confirm: str | None = None,
          label: str = "", css: str = "", icon: str | None = None) -> str:
    attrs = f'hx-post="{url}" hx-target="#status"'
    if vals:
        attrs += f' hx-vals="{_attr(vals)}"'
    if confirm:
        attrs += f' hx-confirm="{_attr(confirm)}"'
    if icon:  # compact button: the label becomes the tooltip and accessible name
        return (f'<button type="button" class="icon-btn {css}" title="{_attr(label)}" '
                f'aria-label="{_attr(label)}" {attrs}>{_icon(icon)}</button>')
    return f'<button type="button" class="{css}" {attrs}>{escape(label)}</button>'


_STATUS_LETTERS = {
    "modified": "M", "added": "A", "deleted": "D", "renamed": "R", "copied": "C",
    "type changed": "T", "untracked": "U", "conflict": "!",
}


def _status_badge(status: str) -> str:
    """One-letter status (M, A, D…) with the full word as tooltip."""
    letter = _STATUS_LETTERS.get(status, status[:1].upper())
    return (f'<span class="tag {escape(status.replace(" ", "-"))}" title="{_attr(status)}">'
            f'{escape(letter)}<span class="sr-only"> {escape(status)}</span></span>')


def render_status(s: RepoStatus, error: str | None = None, notice: str | None = None,
                  hint: str | None = None, action: dict | None = None,
                  branch_lists: tuple[list, list[str]] | None = None,
                  stashes: list | None = None, tag_list: list | None = None) -> str:
    if s.branch:
        head = f'<span class="branch" title="Current branch">{_icon("branch", 13)}{escape(s.branch)}</span>'
    elif s.detached:
        head = f'<span class="branch detached">detached at {escape(s.head_commit[:7])}</span>'
    else:
        head = '<span class="branch">no branch</span>'

    if s.head_commit:
        commit = f'<code>{escape(s.head_commit[:7])}</code> {escape(s.head_summary or "")}'
    else:
        commit = '<span class="muted">No commits yet</span>'

    if s.upstream:
        sync = f'<span title="Upstream branch">↔ <code>{escape(s.upstream)}</code></span>'
    elif s.branch:
        sync = '<span class="muted">no upstream</span>'
    else:
        sync = ""

    can_stash = bool(s.staged or s.unstaged or s.untracked) and not s.conflicted and bool(s.head_commit)
    toolbar = (
        f'<div id="toolbar" class="toolbar" hx-swap-oob="true" data-branch="{_attr(s.branch or "")}" '
        f'data-staged="{len(s.staged)}" '
        f'data-repo-root="{_attr(s.root)}">'
        f'<div class="tb-head">{head} <span class="tb-commit" title="{_attr(s.head_summary or "")}">{commit}</span>'
        f'{f"<span class=tb-sync>{sync}</span>" if sync else ""}</div>'
        f'{_sync_toolbar(s, can_stash)}</div>'
    )
    parts = [
        toolbar,
        _error_banner(error, hint, action) if error else "",
        f'<div class="banner notice" role="status">{escape(notice)}</div>' if notice else "",
        _operation_box(s),
    ]
    if s.clean:
        parts.append(f'<p class="clean">{_icon("check", 16)}<span>Working tree clean</span></p>')
    else:
        parts += [
            _file_list("Conflicts", s.conflicted, "conflict"),
            _file_list("Staged", s.staged, "staged"),
            _file_list("Changes", s.unstaged, "unstaged"),
            _file_list("Untracked", s.untracked, "untracked"),
        ]
    if stashes is not None:
        parts.append(_stash_list(s, stashes))
    if branch_lists is not None:
        parts.append(_branch_lists(s, *branch_lists))
    if tag_list:
        parts.append(_tag_list(tag_list))
    return "\n".join(p for p in parts if p)


def _stash_list(s: RepoStatus, stashes: list) -> str:
    if not stashes:
        return ""
    items = []
    for st in stashes:
        vals = {"index": str(st.index), "sha": st.sha}
        actions = (
            _post("/actions/stash/apply", vals, label="Apply", css="small")
            + _post("/actions/stash/pop", vals, label="Pop", css="small")
            + _post("/actions/stash/drop", vals, label="Drop", css="small danger",
                    confirm=f"Delete {st.ref} ({st.title})?\n\nIts changes will be lost.")
        )
        where = f"on {escape(st.branch)} · " if st.branch else ""
        url = f"/partials/stash?index={st.index}&sha={st.sha}"
        items.append(
            f'<li class="stash-item"><button type="button" class="file" title="{_attr(st.message)}" '
            f'hx-get="{_attr(url)}" hx-target="#detail">'
            f'<span class="stash-title">{escape(st.title)}</span></button>'
            f'<span class="stash-meta-row"><span class="muted stash-meta">{where}{st.date}</span>'
            f'<span class="actions">{actions}</span></span></li>'
        )
    return (f'<section class="files stashes" {_collapsible("stashes", len(stashes))}>'
            f'<h2>{_section_toggle("Stashes", len(stashes))}</h2>'
            f'<ul>{"".join(items)}</ul></section>')


def delete_tag_question(name: str) -> str:
    return (f"Delete the tag {name}?\n\nOnly the tag on your computer is deleted. "
            "If you pushed it, it stays on the remote.")


def _tag_list(tag_list: list) -> str:
    icon = _icon("tag", 12).replace('class="icon"', 'class="icon tag-icon"', 1)
    items = []
    for t in tag_list:
        title = f"{t.name} — {t.message}" if t.message else f"{t.name} (at {t.sha[:7]})"
        actions = (_post("/actions/tag/push", {"name": t.name}, label="Push tag to the remote", icon="push")
                   + _post("/actions/tag/delete", {"name": t.name}, label="Delete tag", css="danger",
                           icon="trash", confirm=delete_tag_question(t.name)))
        items.append(
            f'<li class="branch-item tag-item" data-ref-kind="tag" data-ref-name="{_attr(t.name)}" '
            f'data-ref-sha="{_attr(t.sha)}">{icon}'
            f'<button type="button" class="file" title="{_attr(title)}" data-show-sha="{_attr(t.sha)}">'
            f'{_wrap(t.name)}</button><span class="actions">{actions}</span></li>'
        )
    return (f'<section class="files tags" {_collapsible("tags", len(tag_list))}>'
            f'<h2>{_section_toggle("Tags", len(tag_list))}</h2>'
            f'<ul>{"".join(items)}</ul></section>')


def _operation_box(s: RepoStatus) -> str:
    if not s.operation:
        return ""
    names = {"merge": "Merge", "cherry-pick": "Cherry-pick", "revert": "Revert", "rebase": "Rebase"}
    title = names[s.operation]
    n = len(s.conflicted)
    if n:
        what = f"{n} file{'s' if n != 1 else ''} with conflicts. Fix them in your editor, save, then:"
    else:
        what = "No conflicts left. Finish it:"
    finish = ("" if s.operation == "rebase" else
              _post("/actions/operation/continue", label="Mark resolved and commit", css="primary"))
    abort = _post("/actions/operation/abort", label=f"Abort {s.operation}", css="danger",
                  confirm=f"Abort the {s.operation}?\n\nAll changes it made, including conflict "
                          "fixes you did, will be undone.")
    return (f'<section class="operation" role="alert"><strong>{title} in progress</strong>'
            f'<p>{escape(what)}</p><div class="op-buttons">{finish}{abort}</div></section>')


def _branch_lists(s: RepoStatus, local: list, remote: list[str]) -> str:
    items = []
    for b in local:
        counts = ""
        if b.gone:
            counts = '<span class="muted" title="Deleted on the remote">gone</span>'
        elif b.ahead or b.behind:
            counts = f'<span class="muted">↑{b.ahead} ↓{b.behind}</span>'
        on_remote = "gone" if b.gone else bool(b.upstream)
        where = where_icons(True, on_remote)
        where_title = ("on your computer; deleted on the remote" if b.gone else
                       f"on your computer and on {b.upstream.split('/')[0]} ({b.upstream})" if b.upstream else
                       "only on your computer (not pushed yet)")
        where = where.replace('class="where"', f'class="where" title="{_attr(where_title)}"', 1)
        if b.current:
            items.append(f'<li class="branch-item current" data-ref-kind="head" data-ref-name="{_attr(b.name)}">'
                         f'{where}<span class="file" title="{_attr(b.name)}">● {_wrap(b.name)}</span>{counts}</li>')
            continue
        actions = ""
        if s.branch:
            actions += _post("/actions/branch/merge", {"name": b.name}, label=f"Merge into {s.branch}",
                             icon="merge", confirm=f"Merge {b.name} into {s.branch}?")
        actions += _post("/actions/branch/delete", {"name": b.name}, label="Delete branch",
                         css="danger", icon="trash", confirm=f"Delete the branch {b.name}?")
        items.append(
            f'<li class="branch-item" data-ref-kind="branch" data-ref-name="{_attr(b.name)}">{where}'
            f'<button type="button" class="file" title="Switch to {_attr(b.name)}" '
            f'hx-post="/actions/branch/checkout" hx-target="#status" hx-vals="{_attr({"name": b.name})}">'
            f'{_wrap(b.name)}</button>{counts}<span class="actions">{actions}</span></li>'
        )
    html = (f'<section class="files branches" {_collapsible("branches", len(local))}>'
            f'<h2>{_section_toggle("Branches", len(local))}</h2>'
            f'<ul>{"".join(items)}</ul></section>' if local else "")
    if remote:
        tracked = {b.upstream for b in local if b.upstream}

        def rwhere(r: str) -> str:
            title = ("on the remote and on your computer" if r in tracked else
                     "only on the remote (click to get a local copy)")
            return where_icons(r in tracked, True).replace(
                'class="where"', f'class="where" title="{_attr(title)}"', 1)

        ritems = "".join(
            f'<li class="branch-item" data-ref-kind="remote" data-ref-name="{_attr(r)}">{rwhere(r)}'
            f'<button type="button" class="file" title="Check out as a local branch" '
            f'hx-post="/actions/branch/checkout" hx-target="#status" hx-vals="{_attr({"name": r})}">'
            f'{_wrap(r)}</button><span class="actions">'
            + _post("/actions/branch/delete-remote", {"name": r}, label="Delete on the remote",
                    css="danger", icon="trash", confirm=delete_remote_question(r))
            + '</span></li>'
            for r in remote
        )
        html += (f'<details class="files remotes" id="remote-branches"><summary>Remote branches '
                 f'<span class="count">{len(remote)}</span></summary><ul>{ritems}</ul></details>')
    return html


def delete_remote_question(name: str) -> str:
    remote, _, branch = name.partition("/")
    return (f"Delete {branch} on {remote}?\n\nThe branch is removed from the server for everyone. "
            "Commits that are only on it may be lost. Your local branches are kept.")


def _prompt_button(url: str, field: str, label: str, question: str, css: str = "small",
                   icon: str | None = None, ok: str | None = None) -> str:
    """A button that asks a question, then POSTs the answer as a form field.

    (htmx's hx-prompt sends the answer URL-encoded in a header, which mangles
    names like "feature/x"; the page script sends it in the body instead.)
    """
    inner = f'{_icon(icon, 15)}<span>{escape(label)}</span>' if icon else escape(label)
    ok_attr = f' data-prompt-ok="{_attr(ok)}"' if ok else ""
    return (f'<button type="button" class="{css}" data-prompt-url="{_attr(url)}" '
            f'data-prompt-field="{_attr(field)}" data-prompt="{_attr(question)}"{ok_attr}>{inner}</button>')


def _error_banner(error: str, hint: str | None, action: dict | None = None) -> str:
    button = ""
    if action:
        button = ('<div class="banner-action">'
                  + _post(action["url"], action.get("vals"), confirm=action.get("confirm"),
                          label=action["label"], css="danger")
                  + "</div>")
    if hint:
        return (f'<div class="banner error" role="alert"><strong>{escape(hint)}</strong>'
                f'<details><summary>Git said</summary><pre>{escape(error)}</pre></details>{button}</div>')
    return f'<div class="banner error" role="alert">{escape(error)}{button}</div>'


def _sync_toolbar(s: RepoStatus, can_stash: bool = False) -> str:
    def btn(url: str, label: str, title: str, icon: str, css: str = "", confirm: str | None = None,
            count: int = 0) -> str:
        extra = f' hx-confirm="{_attr(confirm)}"' if confirm else ""
        badge = f'<span class="badge" aria-label="{count} commits">{count}</span>' if count else ""
        return (f'<button type="button" class="tb-btn {css}" hx-post="{url}" hx-target="#status" '
                f'hx-disabled-elt=".sync-bar button" title="{_attr(title)}"{extra}>'
                f'{_icon(icon, 15)}<span>{label}</span>{badge}</button>')

    buttons = [
        '<div class="btn-group" role="group" aria-label="Sync">',
        btn("/actions/fetch", "Fetch", "Download new commits from all remotes without changing your files",
            "fetch"),
        btn("/actions/pull", "Pull", "Fetch and bring the upstream's new commits into this branch", "pull",
            count=s.behind),
        btn("/actions/push", "Push", "Upload this branch's commits to the remote", "push",
            css="primary" if s.ahead and not s.behind else "", count=s.ahead),
        "</div>",
    ]
    if s.ahead and s.behind:  # diverged: the only case where force push makes sense
        buttons.append(btn(
            "/actions/force-push", "Force push", "Overwrite the remote branch (uses --force-with-lease)",
            "force", css="danger",
            confirm=(f"Force push {s.branch} to {s.upstream}?\n\n"
                     f"The remote's {s.behind} commit(s) that you don't have will be removed "
                     "from the branch. Only do this if you rewrote your own history (amend/rebase).\n\n"
                     "gitlane uses --force-with-lease, so it stops if the remote changed "
                     "since your last fetch."),
        ))
    tools = []
    if s.head_commit:
        tools.append(_prompt_button("/actions/branch/create", "name", "New branch",
                                    "New branch\n\nIt starts from the current commit, and gitlane "
                                    "switches to it. Name:",
                                    css="tb-btn", icon="branch", ok="Create branch"))
    if can_stash:
        tools.append(_prompt_button("/actions/stash/save", "message", "Stash changes",
                                    "Stash your changes\n\nAll changes, including untracked files, are "
                                    "put aside and your working tree becomes clean. Message (optional):",
                                    css="tb-btn", icon="stash", ok="Stash"))
    sep = '<span class="tb-sep" aria-hidden="true"></span>' if tools else ""
    return (f'<div class="sync-bar">{"".join(buttons)}{sep}{"".join(tools)}'
            '<span class="spinner" aria-hidden="true"></span></div>')


COLLAPSE_AFTER = 15  # long lists start minimized (the user's own choice wins)
_AUTO_COLLAPSE = {"untracked", "branches", "stashes", "tags"}


def _collapsible(section: str, count: int) -> str:
    auto = section in _AUTO_COLLAPSE and count > COLLAPSE_AFTER
    return f'data-section="{_attr(section)}" data-auto-collapse="{1 if auto else 0}"'


def _section_toggle(title: str, count: int) -> str:
    """Section header that minimizes/expands its list (the page script handles clicks)."""
    return (f'<button type="button" class="sec-toggle" aria-expanded="true" title="Minimize or expand">'
            f'{_icon("chevron", 12)}<span>{escape(title)}</span> <span class="count">{count}</span></button>')


def _file_list(title: str, files: list[FileChange], kind: str) -> str:
    if not files:
        return ""
    if kind == "staged":
        bulk = _post("/actions/unstage-all", label="Unstage all", css="small")
    elif kind in ("unstaged", "untracked"):
        bulk = _post("/actions/stage-all", label="Stage all", css="small")
    else:
        bulk = ""

    items = []
    for f in files:
        name = _wrap(f.path)
        if f.orig_path:
            name = f"{_wrap(f.orig_path)} → {name}"
        diff_url = f"/partials/diff?path={quote(f.path, safe='')}&staged={1 if kind == 'staged' else 0}"
        actions = ""
        if kind == "staged":
            vals = {"path": f.path, **({"orig": f.orig_path} if f.orig_path else {})}
            actions = _post("/actions/unstage", vals, label="Unstage", icon="minus")
        elif kind == "unstaged":
            actions = (
                _post("/actions/discard", {"path": f.path},
                      confirm=f"Discard your changes to {f.path}?\n\nThis cannot be undone.",
                      label="Discard changes", css="danger", icon="undo")
                + _post("/actions/stage", {"path": f.path}, label="Stage", icon="plus")
            )
        elif kind == "untracked":
            actions = (
                _post("/actions/discard", {"path": f.path},
                      confirm=f"Delete the untracked file {f.path}?\n\nThis cannot be undone.",
                      label="Delete file", css="danger", icon="trash")
                + _post("/actions/stage", {"path": f.path}, label="Stage", icon="plus")
            )
        file_btn = "" if kind == "conflict" else f' hx-get="{_attr(diff_url)}" hx-target="#detail"'
        items.append(
            f'<li>{_status_badge(f.status)}'
            f'<button type="button" class="file" title="{_attr(f.path)}"{file_btn}>{name}</button>'
            f'<span class="actions">{actions}</span></li>'
        )
    return (
        f'<section class="files {kind}" {_collapsible(kind, len(files))}>'
        f'<h2>{_section_toggle(title, len(files))}{bulk}</h2>'
        f'<ul>{"".join(items)}</ul></section>'
    )


def render_picker(recent: list[Path], listing, error: str | None = None) -> str:
    """The "+" tab: recent repos, a folder browser and a path field."""
    def open_btn(path: Path, label: str, css: str = "") -> str:
        return (f'<button type="button" class="{css}" data-open-path="{_attr(str(path))}" '
                f'title="{_attr(str(path))}">{label}</button>')

    parts = ['<div class="picker-inner"><h1>Open a repository</h1>']
    if error:
        parts.append(f'<div class="banner error" role="alert">{escape(error)}</div>')
    parts.append(
        '<form class="picker-path" data-open-form>'
        '<label for="open-path">Path</label>'
        f'<input id="open-path" name="path" type="text" autocomplete="off" spellcheck="false" '
        f'placeholder="{_attr(str(Path.home() / "dev" / "project"))}">'
        '<button type="submit" class="primary">Open</button></form>'
    )
    if recent:
        repo_icon = _icon("repo")
        items = "".join(
            f'<li>{open_btn(p, f"{repo_icon} <strong>{escape(p.name)}</strong> <span class=muted>{_wrap(str(p.parent))}</span>", "file")}</li>'
            for p in recent
        )
        parts.append(f'<section class="files recent"><h2>Recent</h2><ul>{items}</ul></section>')

    crumbs, acc = [], None
    for part in listing.path.parts:
        acc = Path(part) if acc is None else acc / part
        crumbs.append(f'<button type="button" class="crumb" hx-get="/partials/picker?path={quote(str(acc), safe="")}" '
                      f'hx-target="#picker">{escape(part.rstrip("/") or "/")}</button>')
    here = (open_btn(listing.path, "Open this folder", "small primary") if listing.is_repo else "")
    rows = []
    if listing.parent:
        rows.append(f'<li><button type="button" class="file" hx-get="/partials/picker?path='
                    f'{quote(str(listing.parent), safe="")}" hx-target="#picker">{_icon("up")} ..</button></li>')
    for f in listing.folders:
        if f.is_repo:
            label = f'{_icon("repo")} {_wrap(f.name)} <span class=repo-mark>git</span>'
            rows.append(f'<li class="is-repo">{open_btn(f.path, label, "file")}'
                        f'<span class="actions"><button type="button" class="icon-btn" title="Show folders inside" '
                        f'aria-label="Show folders inside {_attr(f.name)}" hx-get="/partials/picker?path='
                        f'{quote(str(f.path), safe="")}" hx-target="#picker">{_icon("chevron")}</button></span></li>')
        else:
            rows.append(f'<li><button type="button" class="file" hx-get="/partials/picker?path='
                        f'{quote(str(f.path), safe="")}" hx-target="#picker">{_icon("folder")} {_wrap(f.name)}</button></li>')
    if not listing.folders:
        rows.append('<li class="muted empty">No folders here.</li>')
    more = '<p class="muted">Too many folders; showing the first ones.</p>' if listing.truncated else ""
    parts.append(
        f'<section class="files browser"><h2>Browse <span class="crumbs">{crumbs[0]}{"/".join(crumbs[1:])}</span>{here}</h2>'
        f'<ul>{"".join(rows)}</ul>{more}</section>'
        '<p class="muted">Git repositories are marked <span class="repo-mark">git</span>. '
        'Click one to open it in a new tab.</p></div>'
    )
    return "".join(parts)


def blob_url(rev: str, path: str) -> str:
    repo = quote(_current_repo.get() or "", safe="")
    return f"/blob?rev={quote(rev, safe='')}&path={quote(path, safe='')}&repo={repo}"


def render_images(before: str | None, after: str | None) -> str:
    def fig(label: str, url: str | None) -> str:
        if url is None:
            return f'<figure><figcaption>{label}</figcaption><div class="no-image">No file</div></figure>'
        u = _attr(url)
        return (f'<figure><figcaption>{label}</figcaption>'
                f'<a href="{u}" target="_blank" rel="noopener" title="Open full size">'
                f'<img src="{u}" alt="{label} version" loading="lazy"></a></figure>')
    return f'<div class="image-compare">{fig("Before", before)}{fig("After", after)}</div>'


def render_diff(d: FileDiff, where: str | None = None,
                images: tuple[str | None, str | None] | None = None) -> str:
    where = where or ("staged" if d.staged else "working tree")
    head = f'<h2 class="diff-title">{_wrap(d.path)} <span class="muted">({where})</span></h2>'
    if images:
        head += render_images(*images)
        if d.binary or d.empty:
            return head
    if d.binary:
        return head + '<p class="muted">Binary file — no text diff.</p>'
    if d.empty:
        return head + '<p class="muted">No changes.</p>'
    rows = []
    for line in d.lines:
        old = "" if line.old is None else line.old
        new = "" if line.new is None else line.new
        if line.kind == "hunk":
            rows.append(f'<tr class="hunk"><td colspan="3">{escape(line.text)}</td></tr>')
        else:
            sign = {"add": "+", "del": "-"}.get(line.kind, " ")
            rows.append(
                f'<tr class="{line.kind}"><td class="ln">{old}</td><td class="ln">{new}</td>'
                f'<td class="code"><span class="sign">{sign}</span>{escape(line.text)}</td></tr>'
            )
    note = (f'<p class="muted">Diff truncated at {git_ops.MAX_DIFF_LINES} lines.</p>'
            if d.truncated else "")
    return head + f'<table class="diff">{"".join(rows)}</table>' + note


# --- graph ----------------------------------------------------------------

LANE_W = 14
ROW_H = 28
N_COLORS = 8


def _x(col: int) -> int:
    return col * LANE_W + LANE_W // 2


def _segment(a: int, b: int, color: int, y0: int, y1: int) -> str:
    cls = f"l{color % N_COLORS}"
    if a == b:
        return f'<line class="{cls}" x1="{_x(a)}" y1="{y0}" x2="{_x(b)}" y2="{y1}"/>'
    mid = (y0 + y1) / 2
    return (f'<path class="{cls}" d="M{_x(a)} {y0} C{_x(a)} {mid} {_x(b)} {mid} {_x(b)} {y1}"/>')


def render_row_svg(row: graph.Row) -> str:
    half = ROW_H // 2
    parts = [_segment(a, b, c, 0, half) for a, b, c in row.top]
    parts += [_segment(a, b, c, half, ROW_H) for a, b, c in row.bottom]
    cx, cls = _x(row.col), f"n{row.color % N_COLORS}"
    is_head = any(r.kind in ("head", "detached") for r in row.commit.refs)
    if row.commit.stash is not None:
        parts.append(f'<circle class="{cls} stash-dot" cx="{cx}" cy="{half}" r="5"/>')
    elif len(row.commit.parents) > 1:
        parts.append(f'<circle class="{cls} merge" cx="{cx}" cy="{half}" r="3.5"/>')
    else:
        parts.append(f'<circle class="{cls}" cx="{cx}" cy="{half}" r="4.5"/>')
    if is_head:
        parts.append(f'<circle class="head-ring" cx="{cx}" cy="{half}" r="7.5"/>')
    width = row.width * LANE_W
    return (f'<svg width="{width}" height="{ROW_H}" viewBox="0 0 {width} {ROW_H}" '
            f'aria-hidden="true">{"".join(parts)}</svg>')


def where_icons(local: bool, remote: bool | str) -> str:
    """Laptop = on your computer, cloud = on the remote (both = in sync there).

    ``remote`` may be "gone": the upstream was deleted on the server.
    """
    icons = []
    if local:
        icons.append(_icon("local", 12))
    if remote == "gone":
        icons.append(_icon("cloud-off", 12))
    elif remote:
        icons.append(_icon("cloud", 12))
    return f'<span class="where" aria-hidden="true">{"".join(icons)}</span>' if icons else ""


def _where_text(r: graph.Ref) -> str:
    if r.kind == "remote":
        return f"{r.name} — only on the remote"
    if r.remote:
        return f"{r.name} — on your computer and on {r.remote.split('/')[0]} (same commit)"
    return f"{r.name} — on your computer"


def render_refs(refs: list[graph.Ref]) -> str:
    labels = {
        "head": ("ref ref-head", "●&nbsp;"), "branch": ("ref ref-branch", ""),
        "remote": ("ref ref-remote", ""), "tag": ("ref ref-tag", _icon("tag", 11)),
        "detached": ("ref ref-head", "HEAD"), "stash": ("ref ref-stash", ""),
    }
    out = []
    for r in refs:
        css, prefix = labels[r.kind]
        name = "" if r.kind == "detached" else f'<span class="ref-name">{escape(r.name)}</span>'
        if r.kind in ("head", "branch", "remote"):
            icons = where_icons(local=r.kind != "remote", remote=r.kind == "remote" or bool(r.remote))
            title = _where_text(r)
        else:
            icons, title = "", f"{r.name} ({r.kind})"
        remote_attr = f' data-ref-remote="{_attr(r.remote)}"' if r.remote else ""
        out.append(f'<span class="{css}" title="{_attr(title)}" data-ref-kind="{escape(r.kind)}" '
                   f'data-ref-name="{_attr(r.name)}"{remote_attr}>{icons}{prefix}{name}</span>')
    return "".join(out)


def render_graph_page(page: graph.GraphPage) -> str:
    if not page.rows and page.skip == 0:
        return '<p class="muted empty-graph">No commits yet.</p>'
    rows = []
    for row in page.rows:
        c = row.commit
        if c.stash is not None:
            attrs = f'class="row stash-row" data-stash-index="{c.stash}"'
            subject = f'<span class="subject" title="Stashed changes">{escape(c.subject)}</span>'
        else:
            attrs = f'class="row" data-merge="{1 if len(c.parents) > 1 else 0}"'
            subject = f'<span class="subject">{escape(c.subject)}</span>'
        rows.append(
            f'<div {attrs} data-sha="{c.sha}" tabindex="0">'
            f'<div class="g">{render_row_svg(row)}</div>'
            f'<div class="msg">{render_refs(c.refs)}{subject}</div>'
            f'<div class="author" title="{_attr(c.email)}">{_avatar(c.author, c.email)}'
            f'<span>{escape(c.author)}</span></div>'
            f'<div class="date">{c.date}</div>'
            f'<div class="sha"><code>{c.sha[:7]}</code></div>'
            "</div>"
        )
    more = ""
    if page.has_more:
        nxt = page.skip + len(page.rows)
        more = (f'<div class="more" hx-get="/partials/graph?skip={nxt}" '
                f'hx-trigger="intersect once" hx-swap="outerHTML">Loading more commits…</div>')
    return f'<div class="page" data-graph-width="{page.width}">{"".join(rows)}</div>{more}'


_KEEP_LINE = re.compile(r"^(\s|[-*+>•]\s|\d+[.)]\s|[A-Za-z][\w-]*: \S|```)")


def reflow(body: str) -> str:
    """Undo Git's 72-column hard wrapping so a narrow panel doesn't wrap twice.

    Lines in a paragraph are joined unless they look like lists, quotes, code
    (indented) or trailers ("Co-Authored-By: …"), which keep their own line.
    """
    out = []
    for para in re.split(r"\n\s*\n", body.strip()):
        lines = para.split("\n")
        joined = [lines[0]]
        for line in lines[1:]:
            if _KEEP_LINE.match(line) or _KEEP_LINE.match(joined[-1]) and joined[-1].startswith((" ", "\t")):
                joined.append(line)
            else:
                joined[-1] = joined[-1].rstrip() + " " + line.strip()
        out.append("\n".join(joined))
    return "\n\n".join(out)


def _avatar(name: str, email: str, size: str = "") -> str:
    """Initials in a circle; the color comes from the e-mail so it's stable per person."""
    words = [w for w in name.replace(".", " ").replace("_", " ").split() if w[:1].isalnum()]
    initials = (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper() if words else "?"
    hue = sum((email or name).lower().encode()) % 8
    return f'<span class="avatar a{hue} {size}" aria-hidden="true">{escape(initials)}</span>'


def render_commit(d: graph.CommitDetail) -> str:
    c = d.commit
    parents = " ".join(
        f'<button type="button" class="sha-link" data-sha="{p}"><code>{p[:7]}</code></button>'
        for p in c.parents
    ) or '<span class="muted">none (first commit)</span>'
    files = []
    for f in d.files:
        params = f"sha={c.sha}&path={quote(f.path, safe='')}"
        if f.orig_path:
            params += f"&orig={quote(f.orig_path, safe='')}"
        name = _wrap(f.path) if not f.orig_path else f"{_wrap(f.orig_path)} → {_wrap(f.path)}"
        files.append(
            f'<li>{_status_badge(f.status)}'
            f'<button type="button" class="file" title="{_attr(f.path)}" hx-get="{_attr("/partials/commit-diff?" + params)}" '
            f'hx-target="#commit-diff">{name}</button></li>'
        )
    note = ""
    if len(c.parents) > 1:
        note = f'<p class="muted">Merge commit — changes shown against the first parent {c.parents[0][:7]}.</p>'
    files_html = f'<ul>{"".join(files)}</ul>' if files else '<p class="muted">No file changes.</p>'
    body = f'<div class="body">{escape(reflow(d.body))}</div>' if d.body else ""
    return (
        '<article class="commit-detail">'
        f'<div class="refs">{render_refs(c.refs)}</div>'
        f'<h2 class="subject">{escape(c.subject)}</h2>{body}'
        '<dl>'
        f'<dt>Author</dt><dd class="who">{_avatar(c.author, c.email, "lg")}<span>{escape(c.author)} '
        f'<span class="muted">&lt;{escape(c.email)}&gt;</span></span></dd>'
        f'<dt>Date</dt><dd>{c.date}</dd>'
        f'<dt>Commit</dt><dd><code class="full-sha">{c.sha}</code></dd>'
        f'<dt>Parents</dt><dd>{parents}</dd>'
        '</dl>'
        f'<h3>Files changed <span class="count">{len(d.files)}</span></h3>{note}{files_html}'
        '<div id="commit-diff"></div>'
        '</article>'
    )


def render_stash(d) -> str:
    st = d.stash
    files = []
    for f in d.files:
        url = f"/partials/stash-diff?index={st.index}&sha={st.sha}&path={quote(f.path, safe='')}"
        name = _wrap(f.path) if not f.orig_path else f"{_wrap(f.orig_path)} → {_wrap(f.path)}"
        files.append(
            f'<li>{_status_badge(f.status)}'
            f'<button type="button" class="file" title="{_attr(f.path)}" hx-get="{_attr(url)}" '
            f'hx-target="#commit-diff">{name}</button></li>'
        )
    vals = {"index": str(st.index), "sha": st.sha}
    buttons = (
        _post("/actions/stash/apply", vals, label="Apply", css="")
        + _post("/actions/stash/pop", vals, label="Pop (apply and delete)", css="primary")
        + _post("/actions/stash/drop", vals, label="Drop", css="danger",
                confirm=f"Delete {st.ref} ({st.title})?\n\nIts changes will be lost.")
    )
    files_html = f'<ul>{"".join(files)}</ul>' if files else '<p class="muted">No changes.</p>'
    branch = f'<dt>Branch</dt><dd>{escape(st.branch)}</dd>' if st.branch else ""
    return (
        '<article class="commit-detail stash-detail">'
        f'<div class="refs"><span class="ref ref-stash">{escape(st.ref)}</span></div>'
        f'<h2 class="subject">{escape(st.title)}</h2>'
        f'<dl><dt>Created</dt><dd>{st.date}</dd>{branch}'
        f'<dt>Commit</dt><dd><code class="full-sha">{st.sha}</code></dd></dl>'
        f'<div class="op-buttons">{buttons}</div>'
        f'<h3>Files <span class="count">{len(d.files)}</span></h3>{files_html}'
        '<div id="commit-diff"></div>'
        '</article>'
    )
