# Contributing to gitlane

Thanks for helping! This guide covers setting up the project, running the tests and sending a change.

## Set up

```bash
git clone https://github.com/<you>/gitlane   # your fork
cd gitlane
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,e2e]"
.venv/bin/gitlane ~/some/repo                 # run your local copy
```

`-e` (editable) means your code changes are used right away; just restart `gitlane`.
On Windows use `.venv\Scripts\` instead of `.venv/bin/`.

## Project layout

| Path | What's there |
|---|---|
| `src/gitlane/cli.py` | the `gitlane` command |
| `src/gitlane/server.py` | the web app (FastAPI), security checks, HTML fragments |
| `src/gitlane/git_ops.py` | status, staging, commit, diff, fetch/pull/push |
| `src/gitlane/graph.py` | reading history and laying out the commit graph |
| `src/gitlane/branches.py`, `stash.py` | branch, commit and stash operations |
| `src/gitlane/workspace.py` | tabs, recent repos, folder browser |
| `src/gitlane/static/` | the page (`index.html`), styles, icon, vendored htmx |
| `tests/` | tests (see below) |
| `SPEC.md` | the design decisions and scope |

There is no build step for the front end: plain HTML, CSS and a little JavaScript with htmx.

## Tests

```bash
.venv/bin/pytest                    # everything
.venv/bin/pytest tests/test_e2e.py  # only the browser tests
```

- Tests use **real Git repositories** in temporary folders, never mocks of Git.
- Push/pull tests use a **local bare repository** as the remote — no network needed.
- Browser tests (`test_e2e.py`) drive your installed **Google Chrome** with Playwright and are
  skipped if it's missing.
- Every new feature or fix comes with tests in the same pull request.

## Rules of thumb

- Keep dependencies minimal (today: FastAPI, uvicorn, GitPython).
- Never build shell commands from user input; pass arguments as lists and validate paths and branch names.
- Anything that can lose work (reset --hard, deleting branches, dropping stashes, discarding changes,
  force push) must ask first. Force push is always `--force-with-lease`.
- UI text is plain, friendly English that says what will happen.

## Sending a change

1. Create a branch, make your change with tests, run `.venv/bin/pytest`.
2. Push to your fork and open a **pull request** describing what and why.
3. GitHub Actions runs the tests on Linux, macOS and Windows.

## Screenshots

`docs/screenshots/` is made from a demo repository (never a real one):

```bash
.venv/bin/python scripts/screenshots.py
```

## Releasing (maintainers)

Releases are published to PyPI by GitHub Actions with
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) — no token is stored.

One-time setup on PyPI: add a trusted publisher for project `gitlane`, owner `gustavojoseleite`,
repository `gitlane`, workflow `release.yml`, environment `pypi`. In GitHub, create the `pypi`
environment under *Settings → Environments*.

For each release:

1. Set the version in **both** `pyproject.toml` and `src/gitlane/__init__.py` (a test checks they match).
2. Add the changes to `CHANGELOG.md`.
3. Commit, then tag and push: `git tag v0.1.0 && git push origin main v0.1.0`
4. The *Release* workflow checks the tag matches the version, builds and publishes.
