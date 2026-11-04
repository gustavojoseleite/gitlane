# gitlane — Specification

> **For the implementing agent:** read this whole file before writing code. These decisions are settled; do not change them without asking the project owner. When something is not covered here, ask instead of guessing.

## 1. Goal

A **free, open-source (MIT) Git client** that runs as a local web app. The user types `gitlane` inside a repository folder; a local server starts and the UI opens in the browser.

**Pitch:** easy and direct to use, centered on an **interactive Git tree** (commit graph).

**Motivation:** GitKraken is too expensive; existing free tools did not satisfy the owner.

## 2. Stack

| Area | Decision |
|---|---|
| Language | Python **3.10+** |
| Server | **FastAPI** + uvicorn |
| Git access | **GitPython**. Calling the `git` CLI (via GitPython's `repo.git.*` or `subprocess`) is allowed where GitPython's API is awkward, e.g. reading the graph with `git log --format=... --parents --all` |
| Frontend | **No build step**: plain HTML + JavaScript + **htmx**, static files shipped inside the Python package. No Node.js. |
| Graph rendering | **SVG**, lane layout computed on the server (or in plain JS), drawn in the browser |
| Tests | **pytest** |
| Packaging | `pyproject.toml`, console script `gitlane`, installable with `pipx install gitlane` / `uv tool install gitlane`, published on PyPI (milestone 7) |
| License | MIT |
| Platforms | Linux tested first; avoid anything that breaks macOS/Windows (use `pathlib`, no shell-specific commands) |

Remote operations (push/pull/fetch) must use the user's existing Git setup (SSH keys, ssh-agent, credential helpers, `~/.gitconfig`). The tool never stores Git credentials in v1.

## 3. Security rules (required from milestone 1)

1. The server binds to **`127.0.0.1` only**, never `0.0.0.0`. Pick a free port automatically (allow `--port` override).
2. On startup, generate a **random secret token** (`secrets.token_urlsafe(32)`). The browser is opened at `http://127.0.0.1:<port>/?token=<token>`; the token is then kept in an HttpOnly, SameSite=Strict cookie.
3. **Every** request (pages and API) without a valid token/cookie gets `403`.
4. Check the `Host` header is `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding protection).
5. State-changing actions use `POST`, never `GET`.
6. Never build shell command strings from user input. Pass arguments as lists; validate branch names (`git check-ref-format --branch`) and file paths (must be inside the repo).

### Dangerous actions — always confirm in the UI first

- Reset `--hard`
- Delete a branch
- Drop a stash
- Discard file changes
- Force push — **always `--force-with-lease`**, never plain `--force`

## 4. Scope

### v1 — in

- **Repos & tabs:** one server holds several repos, each in its own **tab** at the top of the page (see milestone 7)
  - `gitlane <path>` (or `gitlane` inside a repo) opens that repo as a tab; `gitlane` outside a repo opens the repo picker
  - **"+" tab → repo picker:** recent repos (one click), a **folder browser** listed by the server (starts in the home folder, marks Git repos — the browser can't give real folder paths), and a field to paste a path
  - **Open tabs and recent repos are remembered** across restarts in `~/.config/gitlane/`; closing a tab never touches the repo
  - **Toolbar under the tabs** (always visible): current branch with ↑/↓, Fetch, Pull, Push, New branch, Stash, Refresh — these move out of the sidebar
  - Hidden tabs refresh when you switch to them, not in the background
- **Status:** list changed / staged / untracked files
- **Changes:** stage / unstage **whole files**, commit (message box), discard changes (confirm)
- **Diff viewer:** for working-tree files and for files in any commit
- **Sync:** fetch, pull, push, force push with lease (confirm); show Git's error message clearly when something fails
- **Interactive graph:**
  - Commit graph of all branches with colored lanes, branch/tag labels, HEAD marker
  - Click a commit → details panel (message, author, date, hash, changed files); click a file → its diff
  - Right-click a commit → checkout, create branch here, cherry-pick, revert, reset (soft/mixed/hard, hard confirms)
  - Right-click a branch label → checkout, merge into current, delete (confirm)
- **Branches:** create, switch, merge, delete
- **Stash:** save (with optional message), list, apply, pop, drop (confirm)
- **Hide branches (v0.2):** hide a branch (local with its upstream, or remote) or all remote branches from the graph via `git log --exclude`; stored per repo in gitlane's state file (Git is not changed); the current branch is always shown
- **Tags (v0.2):** create at a commit (lightweight, or annotated with a message), list in the sidebar, push one tag, delete locally (confirm), delete on the remote (confirm)
- **Merge conflicts (simple):** when a merge stops with conflicts, show the conflicted files and offer **"Abort merge"** or **"I resolved it in my editor → mark resolved and commit"**
- **Refresh:** UI refreshes after every action, when the browser tab regains focus, and via a Refresh button

### Later (v2+) — out of v1

- Pull requests (list/view/create) via GitHub API with a Personal Access Token stored in the system keyring
- Drag-and-drop on the graph (merge/rebase by dragging)
- Staging individual lines/hunks
- Rebase (interactive or not)
- Built-in merge-conflict editor
- Live refresh by watching the repo folder

Do **not** implement v2 features during v1 milestones.

## 5. Milestones

Build in this order. Each milestone is **done only when its tests pass and the owner has tried it in the browser**. Do not start the next milestone before that.

1. **Skeleton** — package layout, `gitlane` command, FastAPI app with all security rules (§3), browser opens automatically, page shows current branch and repo status (read-only). Clear message if the folder is not a Git repo.
2. **Changes** — stage/unstage files, commit, diff viewer for working-tree files, discard changes (confirm).
3. **Sync** — fetch, pull, push, force push with lease (confirm), readable error display.
4. **Graph** — commit graph with lanes and labels, click a commit → details, files and diffs. Must stay responsive on a repo with ~10,000 commits (load commits in pages, e.g. 300 at a time).
5. **Branches** — create/switch/merge/delete, right-click menus on commits and branch labels, simple merge-conflict state (§4).
6. **Stash** — save/list/apply/pop/drop.
7. **Repos & tabs** — several repos in tabs, repo picker (recent / folder browser / paste path), remembered tabs, toolbar under the tabs (§4).
8. **Polish & release** — README with screenshots, packaging, publish to PyPI.
   *Done locally:* install docs (pipx / uv / uvx), Git checks at startup, `--install-launcher`,
   CI on Linux/macOS/Windows, release workflow with Trusted Publishing, CONTRIBUTING and CHANGELOG.
   *Waiting for the owner's OK:* creating the public GitHub repo and the first PyPI release.

## 6. Testing rules

- Use **pytest** with **real temporary Git repos** (`tmp_path` fixtures), never mocks of Git.
- Push/pull/fetch tests use a **local bare repository as the remote** — no network access in tests.
- Test the HTTP API with FastAPI's `TestClient`, including security tests: missing/wrong token → 403, wrong `Host` → rejected, `GET` cannot change state.
- Every new feature comes with tests in the same change.
- `pytest` must pass before a milestone is declared done.

## 7. Suggested layout

```
gitlane/
├── SPEC.md
├── README.md
├── LICENSE
├── pyproject.toml
├── src/gitlane/
│   ├── __init__.py
│   ├── cli.py          # `gitlane` entry point: find repo, start server, open browser
│   ├── server.py       # FastAPI app, security middleware
│   ├── git_ops.py      # all Git operations (GitPython / CLI)
│   ├── graph.py        # commit graph + lane layout
│   ├── config.py       # recent repos, user config dir
│   └── static/         # HTML, JS, CSS, htmx
└── tests/
```

## 8. Project rules

- Location: `~/dev/gitlane`. Local Git repo only for now; the **public GitHub repo is created later, only with the owner's OK**.
- Keep dependencies minimal (FastAPI, uvicorn, GitPython; htmx vendored as a static file).
- UI text in English.
