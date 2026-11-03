"""FastAPI app and security middleware (SPEC.md §3)."""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict
from html import escape
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, blobs, git_ops, graph
from .git_ops import FileChange, FileDiff, GitError, NotAGitRepo, RepoStatus, get_status, open_repo

COOKIE_NAME = "gitlane_token"
STATIC_DIR = Path(str(resources.files("gitlane") / "static"))
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def create_app(repo_path: str | Path, token: str, port: int) -> FastAPI:
    """Build the app for one repository.

    ``token`` is the secret from the startup URL; ``port`` is used for the
    Host-header check (DNS-rebinding protection).
    """
    repo_path = Path(repo_path)
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
        return await call_next(request)

    def with_repo(fn, *args):
        repo = open_repo(repo_path)
        try:
            return fn(repo, *args)
        finally:
            repo.close()

    def status_response(error: str | None = None, notice: str | None = None,
                        events: tuple[str, ...] = (), hint: str | None = None) -> HTMLResponse:
        html = render_status(with_repo(get_status), error=error, notice=notice, hint=hint)
        headers = {"HX-Trigger": ", ".join(events)} if events else {}
        return HTMLResponse(html, headers=headers)

    async def run_action(fn, *args, notice: str | None = None,
                         extra_events: tuple[str, ...] = ()) -> HTMLResponse:
        try:
            result = with_repo(fn, *args)
        except (GitError, NotAGitRepo) as e:
            return status_response(error=str(e), hint=getattr(e, "hint", None),
                                   events=("gitlane:changed",))
        if callable(notice):
            notice = notice(result)
        return status_response(notice=notice, events=("gitlane:changed", *extra_events))

    # --- pages & reads -----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

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

def _attr(value) -> str:
    """Escape a value for use inside a double-quoted HTML attribute."""
    return escape(value if isinstance(value, str) else json.dumps(value), quote=True)


def _post(url: str, vals: dict | None = None, confirm: str | None = None,
          label: str = "", css: str = "") -> str:
    attrs = f'hx-post="{url}" hx-target="#status"'
    if vals:
        attrs += f' hx-vals="{_attr(vals)}"'
    if confirm:
        attrs += f' hx-confirm="{_attr(confirm)}"'
    return f'<button type="button" class="{css}" {attrs}>{escape(label)}</button>'


def render_status(s: RepoStatus, error: str | None = None, notice: str | None = None,
                  hint: str | None = None) -> str:
    if s.branch:
        head = f'<span class="branch">{escape(s.branch)}</span>'
    elif s.detached:
        head = f'<span class="branch detached">detached at {escape(s.head_commit[:7])}</span>'
    else:
        head = '<span class="branch">no branch</span>'

    if s.head_commit:
        commit = f'<code>{escape(s.head_commit[:7])}</code> {escape(s.head_summary or "")}'
    else:
        commit = '<span class="muted">No commits yet</span>'

    if s.upstream:
        sync = f'tracking <code>{escape(s.upstream)}</code> · ↑{s.ahead} ↓{s.behind}'
    elif s.branch:
        sync = '<span class="muted">no upstream</span>'
    else:
        sync = ""

    parts = [
        _error_banner(error, hint) if error else "",
        f'<div class="banner notice" role="status">{escape(notice)}</div>' if notice else "",
        '<section class="repo">',
        f'<h1>{escape(s.name)}</h1>',
        f'<p class="path">{escape(s.root)}</p>',
        f'<p>{head} {commit}</p>',
        f'<p class="sync">{sync}</p>' if sync else "",
        _sync_toolbar(s),
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


def _error_banner(error: str, hint: str | None) -> str:
    if hint:
        return (f'<div class="banner error" role="alert"><strong>{escape(hint)}</strong>'
                f'<details><summary>Git said</summary><pre>{escape(error)}</pre></details></div>')
    return f'<div class="banner error" role="alert">{escape(error)}</div>'


def _sync_toolbar(s: RepoStatus) -> str:
    def btn(url: str, label: str, title: str, css: str = "", confirm: str | None = None) -> str:
        extra = f' hx-confirm="{_attr(confirm)}"' if confirm else ""
        return (f'<button type="button" class="{css}" hx-post="{url}" hx-target="#status" '
                f'hx-disabled-elt=".sync-bar button" title="{_attr(title)}"{extra}>{label}</button>')

    pull_label = f"Pull ↓{s.behind}" if s.behind else "Pull"
    push_label = f"Push ↑{s.ahead}" if s.ahead else "Push"
    buttons = [
        btn("/actions/fetch", "Fetch", "Download new commits from all remotes without changing your files"),
        btn("/actions/pull", pull_label, "Fetch and bring the upstream's new commits into this branch"),
        btn("/actions/push", push_label, "Upload this branch's commits to the remote",
            css="primary" if s.ahead and not s.behind else ""),
    ]
    if s.ahead and s.behind:  # diverged: the only case where force push makes sense
        buttons.append(btn(
            "/actions/force-push", "Force push", "Overwrite the remote branch (uses --force-with-lease)",
            css="danger",
            confirm=(f"Force push {s.branch} to {s.upstream}?\n\n"
                     f"The remote's {s.behind} commit(s) that you don't have will be removed "
                     "from the branch. Only do this if you rewrote your own history (amend/rebase).\n\n"
                     "gitlane uses --force-with-lease, so it stops if the remote changed "
                     "since your last fetch."),
        ))
    return f'<div class="sync-bar">{"".join(buttons)}<span class="spinner" aria-hidden="true"></span></div>'


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
        name = escape(f.path)
        if f.orig_path:
            name = f"{escape(f.orig_path)} → {name}"
        diff_url = f"/partials/diff?path={quote(f.path, safe='')}&staged={1 if kind == 'staged' else 0}"
        actions = ""
        if kind == "staged":
            vals = {"path": f.path, **({"orig": f.orig_path} if f.orig_path else {})}
            actions = _post("/actions/unstage", vals, label="Unstage", css="small")
        elif kind == "unstaged":
            actions = (
                _post("/actions/discard", {"path": f.path},
                      confirm=f"Discard your changes to {f.path}?\n\nThis cannot be undone.",
                      label="Discard", css="small danger")
                + _post("/actions/stage", {"path": f.path}, label="Stage", css="small")
            )
        elif kind == "untracked":
            actions = (
                _post("/actions/discard", {"path": f.path},
                      confirm=f"Delete the untracked file {f.path}?\n\nThis cannot be undone.",
                      label="Delete", css="small danger")
                + _post("/actions/stage", {"path": f.path}, label="Stage", css="small")
            )
        file_btn = "" if kind == "conflict" else f' hx-get="{_attr(diff_url)}" hx-target="#detail"'
        items.append(
            f'<li><span class="tag {escape(f.status.replace(" ", "-"))}">{escape(f.status)}</span>'
            f'<button type="button" class="file"{file_btn}>{name}</button>'
            f'<span class="actions">{actions}</span></li>'
        )
    return (
        f'<section class="files {kind}"><h2>{title} <span class="count">{len(files)}</span>{bulk}</h2>'
        f'<ul>{"".join(items)}</ul></section>'
    )


def blob_url(rev: str, path: str) -> str:
    return f"/blob?rev={quote(rev, safe='')}&path={quote(path, safe='')}"


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
    head = f'<h2 class="diff-title">{escape(d.path)} <span class="muted">({where})</span></h2>'
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
    if len(row.commit.parents) > 1:
        parts.append(f'<circle class="{cls} merge" cx="{cx}" cy="{half}" r="3.5"/>')
    else:
        parts.append(f'<circle class="{cls}" cx="{cx}" cy="{half}" r="4.5"/>')
    if is_head:
        parts.append(f'<circle class="head-ring" cx="{cx}" cy="{half}" r="7.5"/>')
    width = row.width * LANE_W
    return (f'<svg width="{width}" height="{ROW_H}" viewBox="0 0 {width} {ROW_H}" '
            f'aria-hidden="true">{"".join(parts)}</svg>')


def render_refs(refs: list[graph.Ref]) -> str:
    labels = {
        "head": ("ref head", "●&nbsp;"), "branch": ("ref branch", ""),
        "remote": ("ref remote", ""), "tag": ("ref tag", "🏷&nbsp;"),
        "detached": ("ref head", "HEAD"),
    }
    out = []
    for r in refs:
        css, prefix = labels[r.kind]
        name = "" if r.kind == "detached" else escape(r.name)
        out.append(f'<span class="{css}" title="{escape(r.kind)}">{prefix}{name}</span>')
    return "".join(out)


def render_graph_page(page: graph.GraphPage) -> str:
    if not page.rows and page.skip == 0:
        return '<p class="muted empty-graph">No commits yet.</p>'
    rows = []
    for row in page.rows:
        c = row.commit
        rows.append(
            f'<div class="row" data-sha="{c.sha}" tabindex="0">'
            f'<div class="g">{render_row_svg(row)}</div>'
            f'<div class="msg">{render_refs(c.refs)}<span class="subject">{escape(c.subject)}</span></div>'
            f'<div class="author" title="{_attr(c.email)}">{escape(c.author)}</div>'
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
        name = escape(f.path) if not f.orig_path else f"{escape(f.orig_path)} → {escape(f.path)}"
        files.append(
            f'<li><span class="tag {escape(f.status.replace(" ", "-"))}">{escape(f.status)}</span>'
            f'<button type="button" class="file" hx-get="{_attr("/partials/commit-diff?" + params)}" '
            f'hx-target="#commit-diff">{name}</button></li>'
        )
    note = ""
    if len(c.parents) > 1:
        note = f'<p class="muted">Merge commit — changes shown against the first parent {c.parents[0][:7]}.</p>'
    files_html = f'<ul>{"".join(files)}</ul>' if files else '<p class="muted">No file changes.</p>'
    body = f'<pre class="body">{escape(d.body)}</pre>' if d.body else ""
    return (
        '<article class="commit-detail">'
        f'<div class="refs">{render_refs(c.refs)}</div>'
        f'<h2 class="subject">{escape(c.subject)}</h2>{body}'
        '<dl>'
        f'<dt>Author</dt><dd>{escape(c.author)} <span class="muted">&lt;{escape(c.email)}&gt;</span></dd>'
        f'<dt>Date</dt><dd>{c.date}</dd>'
        f'<dt>Commit</dt><dd><code class="full-sha">{c.sha}</code></dd>'
        f'<dt>Parents</dt><dd>{parents}</dd>'
        '</dl>'
        f'<h3>Files changed <span class="count">{len(d.files)}</span></h3>{note}{files_html}'
        '<div id="commit-diff"></div>'
        '</article>'
    )
