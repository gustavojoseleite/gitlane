from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gitlane.server import COOKIE_NAME, create_app

TOKEN = "secret-token-for-tests"
PORT = 54321
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def repo_path(make_repo):
    path = make_repo()
    (path / "README.md").write_text("changed <b>bold</b>\n")
    (path / "<script>.txt").write_text("x")
    return path


@pytest.fixture
def anon(repo_path):
    """Client without the token cookie."""
    return TestClient(create_app(repo_path, TOKEN, PORT), base_url=BASE, follow_redirects=False)


@pytest.fixture
def client(anon):
    """Client that went through the token login."""
    r = anon.get(f"/?token={TOKEN}")
    assert r.status_code == 303
    return anon


# --- security ---------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/api/status", "/partials/status", "/static/app.css"])
def test_everything_requires_token(anon, path):
    assert anon.get(path).status_code == 403


def test_wrong_token_rejected(anon):
    r = anon.get("/?token=wrong")
    assert r.status_code == 403
    assert COOKIE_NAME not in r.cookies


def test_wrong_cookie_rejected(anon):
    anon.cookies.set(COOKIE_NAME, "wrong")
    assert anon.get("/api/status").status_code == 403


def test_token_exchanged_for_cookie(anon):
    r = anon.get(f"/?token={TOKEN}")
    assert r.status_code == 303
    assert r.headers["location"] == "/"  # token removed from the URL
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie
    assert anon.get("/").status_code == 200


def test_token_in_post_not_accepted(anon):
    assert anon.post(f"/?token={TOKEN}").status_code == 403


@pytest.mark.parametrize("host", [f"evil.com:{PORT}", "127.0.0.1:1", "127.0.0.1", "0.0.0.0:54321"])
def test_bad_host_rejected(client, host):
    r = client.get("/api/status", headers={"host": host})
    assert r.status_code == 403
    assert "Host" in r.text


def test_localhost_host_allowed(client):
    assert client.get("/api/status", headers={"host": f"localhost:{PORT}"}).status_code == 200


def test_get_endpoints_do_not_accept_post(client):
    assert client.post("/api/status").status_code == 405


def test_no_docs_endpoints(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --- content ----------------------------------------------------------------

def test_index_page(client):
    r = client.get("/")
    assert "text/html" in r.headers["content-type"]
    assert "/partials/status" in r.text
    assert client.get("/static/htmx.min.js").status_code == 200


def test_api_status(client, repo_path):
    data = client.get("/api/status").json()
    assert data["branch"] == "main"
    assert data["name"] == repo_path.name
    assert data["clean"] is False
    assert [f["path"] for f in data["unstaged"]] == ["README.md"]
    assert [f["path"] for f in data["untracked"]] == ["<script>.txt"]


def test_partial_status_escapes_html(client):
    html = client.get("/partials/status").text
    assert "main" in html and "Initial commit" in html
    assert "&lt;script&gt;.txt" in html
    assert "<script>" not in html


def test_partial_status_clean(make_repo):
    path = make_repo("clean")
    c = TestClient(create_app(path, TOKEN, PORT), base_url=BASE, follow_redirects=False)
    c.get(f"/?token={TOKEN}")
    assert "Working tree clean" in c.get("/partials/status").text


# --- milestone 2: actions ---------------------------------------------------

def test_actions_require_cookie(anon):
    for path in ("/actions/stage", "/actions/unstage", "/actions/commit",
                 "/actions/discard", "/actions/stage-all", "/actions/unstage-all"):
        assert anon.post(path, data={"path": "README.md"}).status_code == 403


def test_actions_reject_get(client):
    assert client.get("/actions/stage-all").status_code == 405


def test_cross_origin_post_rejected(client):
    r = client.post("/actions/stage-all", headers={"origin": "http://evil.com"})
    assert r.status_code == 403
    assert client.get("/api/status").json()["staged"] == []


def test_same_origin_post_allowed(client):
    r = client.post("/actions/stage-all", headers={"origin": BASE})
    assert r.status_code == 200


def test_stage_then_commit_flow(client, repo_path):
    r = client.post("/actions/stage", data={"path": "README.md"})
    assert r.status_code == 200
    assert "gitlane:changed" in r.headers["hx-trigger"]
    assert [f["path"] for f in client.get("/api/status").json()["staged"]] == ["README.md"]

    r = client.post("/actions/commit", data={"message": "Change readme"})
    assert "Committed" in r.text
    assert "gitlane:committed" in r.headers["hx-trigger"]
    data = client.get("/api/status").json()
    assert data["head_summary"] == "Change readme" and data["staged"] == []


def test_commit_error_shown_in_banner(client):
    r = client.post("/actions/commit", data={"message": "nothing staged"})
    assert r.status_code == 200
    assert 'class="banner error"' in r.text and "Nothing staged" in r.text
    assert "gitlane:committed" not in r.headers.get("hx-trigger", "")


def test_unstage_rename_sends_both_paths(client, repo_path):
    from .conftest import run
    run(repo_path, "checkout", "--", "README.md")
    run(repo_path, "mv", "README.md", "renamed.md")
    html = client.get("/partials/status").text
    assert "orig" in html  # the unstage button carries the original path
    client.post("/actions/unstage", data={"path": "renamed.md", "orig": "README.md"})
    assert client.get("/api/status").json()["staged"] == []


def test_discard_has_confirm_and_works(client, repo_path):
    html = client.get("/partials/status").text
    assert "hx-confirm" in html
    client.post("/actions/discard", data={"path": "README.md"})
    assert (repo_path / "README.md").read_text() == "hello\n"


def test_discard_requires_exactly_one_path(client):
    r = client.post("/actions/discard", data={"path": ["README.md", "<script>.txt"]})
    assert "one file at a time" in r.text


def test_bad_path_shows_error(client):
    r = client.post("/actions/stage", data={"path": "../../etc/passwd"})
    assert "outside the repository" in r.text


def test_diff_partial_escapes_html(client):
    html = client.get("/partials/diff", params={"path": "README.md"}).text
    assert "&lt;b&gt;bold&lt;/b&gt;" in html
    assert "<b>" not in html
    assert 'class="add"' in html and 'class="del"' in html


def test_diff_partial_bad_path(client):
    html = client.get("/partials/diff", params={"path": "/etc/passwd"}).text
    assert 'class="error"' in html


# --- milestone 3: sync --------------------------------------------------------

@pytest.mark.parametrize("path", ["/actions/fetch", "/actions/pull", "/actions/push", "/actions/force-push"])
def test_sync_actions_require_cookie(anon, path):
    assert anon.post(path).status_code == 403


@pytest.mark.parametrize("path", ["/actions/fetch", "/actions/pull", "/actions/push", "/actions/force-push"])
def test_sync_actions_reject_get(client, path):
    assert client.get(path).status_code == 405


def test_sync_toolbar_and_error_hint_without_remote(client):
    html = client.get("/partials/status").text
    assert "/actions/fetch" in html and "/actions/pull" in html and "/actions/push" in html
    assert "/actions/force-push" not in html  # only shown when diverged
    r = client.post("/actions/push")
    assert 'class="banner error"' in r.text and "no remote" in r.text


def test_force_push_button_only_when_diverged(tmp_path, make_repo):
    from .conftest import run
    path = make_repo("diverged")
    run(tmp_path, "init", "-q", "--bare", "r.git")
    run(path, "remote", "add", "origin", str(tmp_path / "r.git"))
    run(path, "push", "-q", "-u", "origin", "main")
    (path / "a").write_text("a")
    run(path, "add", "a")
    run(path, "commit", "-q", "-m", "a")
    run(path, "push", "-q")
    run(path, "commit", "-q", "--amend", "-m", "amended")  # now ahead 1, behind 1
    c = TestClient(create_app(path, TOKEN, PORT), base_url=BASE, follow_redirects=False)
    c.get(f"/?token={TOKEN}")
    html = c.get("/partials/status").text
    assert "/actions/force-push" in html and "hx-confirm" in html and "force-with-lease" in html
    r = c.post("/actions/force-push")
    assert "Force pushed main to origin/main." in r.text


def test_hint_banner_shows_git_output(client, repo_path):
    from .conftest import run
    run(repo_path, "remote", "add", "origin", "/nonexistent/repo.git")
    run(repo_path, "branch", "--set-upstream-to", "main")  # any upstream so pull runs
    r = client.post("/actions/fetch")
    assert "<details>" in r.text and "Git said" in r.text


# --- milestone 4: graph -------------------------------------------------------

@pytest.mark.parametrize("path", ["/partials/graph", "/partials/commit?sha=abc", "/api/refs-version",
                                  "/partials/commit-diff?sha=abc&path=x"])
def test_graph_endpoints_require_cookie(anon, path):
    assert anon.get(path).status_code == 403


def test_graph_partial(client, repo_path):
    from .conftest import run
    run(repo_path, "commit", "-q", "--allow-empty", "-m", "<b>not bold</b>")
    html = client.get("/partials/graph").text
    assert html.count('class="row"') == 2
    assert "&lt;b&gt;not bold&lt;/b&gt;" in html and "<b>" not in html
    assert "<svg" in html and 'data-graph-width="1"' in html
    assert 'class="ref ref-head"' in html
    assert 'class="more"' not in html  # everything fits in one page


def test_graph_infinite_scroll_sentinel(client, repo_path, monkeypatch):
    from gitlane import graph
    from .conftest import run
    for i in range(4):
        run(repo_path, "commit", "-q", "--allow-empty", "-m", f"c{i}")
    monkeypatch.setattr(graph, "PAGE_SIZE", 2)
    monkeypatch.setattr(graph.graph_page, "__defaults__", (0, 2))
    html = client.get("/partials/graph").text
    assert html.count('class="row"') == 2
    assert 'hx-get="/partials/graph?skip=2"' in html and 'hx-trigger="intersect once"' in html
    last = client.get("/partials/graph?skip=4").text
    assert last.count('class="row"') == 1 and 'class="more"' not in last


def test_commit_detail_partial(client, repo_path):
    from .conftest import run
    sha = run(repo_path, "rev-parse", "HEAD")
    html = client.get(f"/partials/commit?sha={sha}").text
    assert "Initial commit" in html and sha in html
    assert "/partials/commit-diff?sha=" in html and "README.md" in html
    diff = client.get("/partials/commit-diff", params={"sha": sha, "path": "README.md"}).text
    assert 'class="add"' in diff and f"commit {sha[:7]}" in diff


def test_commit_detail_bad_sha(client):
    assert 'class="error"' in client.get("/partials/commit?sha=--all").text
    assert 'class="error"' in client.get("/partials/commit-diff?sha=zzz&path=a").text


def test_refs_version_endpoint(client, repo_path):
    from .conftest import run
    v1 = client.get("/api/refs-version").json()["version"]
    run(repo_path, "commit", "-q", "--allow-empty", "-m", "x")
    assert client.get("/api/refs-version").json()["version"] != v1


def test_empty_repo_graph(make_repo):
    path = make_repo("empty", commit=False)
    c = TestClient(create_app(path, TOKEN, PORT), base_url=BASE, follow_redirects=False)
    c.get(f"/?token={TOKEN}")
    assert "No commits yet" in c.get("/partials/graph").text


# --- image previews -----------------------------------------------------------

from .test_blobs import PNG  # noqa: E402


def test_blob_endpoint_serves_images_safely(client, repo_path):
    (repo_path / "logo.png").write_bytes(PNG)
    r = client.get("/blob", params={"rev": "WORKTREE", "path": "logo.png"})
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["content-type"] == "image/png"
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_blob_endpoint_refuses_non_images_and_bad_input(anon, client):
    assert client.get("/blob", params={"rev": "WORKTREE", "path": "README.md"}).status_code == 415
    assert client.get("/blob", params={"rev": "WORKTREE", "path": "../x.png"}).status_code == 400
    assert client.get("/blob", params={"rev": "WORKTREE", "path": "missing.png"}).status_code == 404
    assert client.get("/blob", params={"rev": "--all", "path": "a.png"}).status_code == 400


def test_blob_requires_cookie(make_repo):
    path = make_repo("cookieless")
    c = TestClient(create_app(path, TOKEN, PORT), base_url=BASE)
    assert c.get("/blob", params={"rev": "WORKTREE", "path": "a.png"}).status_code == 403


def test_untracked_image_preview(client, repo_path):
    (repo_path / "new.png").write_bytes(PNG)
    html = client.get("/partials/diff", params={"path": "new.png"}).text
    assert 'class="image-compare"' in html
    assert "No file" in html  # nothing before
    assert "/blob?rev=WORKTREE&amp;path=new.png" in html
    assert "Binary file" not in html


def test_commit_image_preview_before_and_after(client, repo_path):
    from .conftest import run
    (repo_path / "pic.png").write_bytes(PNG)
    run(repo_path, "add", "pic.png")
    run(repo_path, "commit", "-q", "-m", "add pic")
    first = run(repo_path, "rev-parse", "HEAD")
    (repo_path / "pic.png").write_bytes(PNG + b"v2")
    run(repo_path, "commit", "-q", "-am", "change pic")
    second = run(repo_path, "rev-parse", "HEAD")

    added = client.get("/partials/commit-diff", params={"sha": first, "path": "pic.png"}).text
    assert "No file" in added and f"/blob?rev={first}&amp;path=pic.png" in added

    changed = client.get("/partials/commit-diff", params={"sha": second, "path": "pic.png"}).text
    assert f"/blob?rev={first}&amp;path=pic.png" in changed
    assert f"/blob?rev={second}&amp;path=pic.png" in changed
    r = client.get("/blob", params={"rev": second, "path": "pic.png"})
    assert r.content == PNG + b"v2" and "max-age" in r.headers["cache-control"]


def test_staged_image_preview(client, repo_path):
    from .conftest import run
    (repo_path / "s.png").write_bytes(PNG)
    run(repo_path, "add", "s.png")
    html = client.get("/partials/diff", params={"path": "s.png", "staged": 1}).text
    assert "/blob?rev=INDEX&amp;path=s.png" in html and "No file" in html


# --- summary + description -------------------------------------------------------

def test_commit_with_summary_and_description(client, repo_path):
    from .conftest import run
    client.post("/actions/stage", data={"path": "README.md"})
    r = client.post("/actions/commit", data={"summary": "  Fix login bug ",
                                             "description": "The token expired too early.\nNow 1h."})
    assert "Committed" in r.text
    assert run(repo_path, "log", "-1", "--format=%s") == "Fix login bug"
    assert run(repo_path, "log", "-1", "--format=%b") == "The token expired too early.\nNow 1h."


def test_commit_summary_only(client, repo_path):
    from .conftest import run
    client.post("/actions/stage", data={"path": "README.md"})
    client.post("/actions/commit", data={"summary": "Just a title", "description": "  "})
    assert run(repo_path, "log", "-1", "--format=%B") == "Just a title"


def test_commit_description_without_summary_rejected(client):
    client.post("/actions/stage", data={"path": "README.md"})
    r = client.post("/actions/commit", data={"summary": " ", "description": "body only"})
    assert 'class="banner error"' in r.text and "message" in r.text


def test_index_has_summary_and_description_fields(client):
    html = client.get("/").text
    assert 'name="summary"' in html and 'name="description"' in html
    assert 'name="message"' not in html


# --- milestone 5: branches ------------------------------------------------------

M5_ACTIONS = ["/actions/branch/create", "/actions/branch/checkout", "/actions/branch/merge",
              "/actions/branch/delete", "/actions/commit/checkout", "/actions/commit/cherry-pick",
              "/actions/commit/revert", "/actions/commit/reset", "/actions/operation/abort",
              "/actions/operation/continue"]


@pytest.mark.parametrize("path", M5_ACTIONS)
def test_m5_actions_require_cookie(anon, path):
    assert anon.post(path, data={"name": "x", "sha": "abc"}).status_code == 403


@pytest.mark.parametrize("path", M5_ACTIONS)
def test_m5_actions_reject_get(client, path):
    assert client.get(path).status_code == 405


def test_branch_list_in_sidebar(client, repo_path):
    from .conftest import run
    run(repo_path, "branch", "feature")
    html = client.get("/partials/status").text
    assert 'data-ref-kind="head" data-ref-name="main"' in html
    assert 'data-ref-kind="branch" data-ref-name="feature"' in html
    assert "data-prompt-url" in html and "New branch" in html
    assert 'data-branch="main"' in html


def test_create_branch_with_slash_and_accents(client, repo_path):
    from .conftest import run
    r = client.post("/actions/branch/create", data={"name": "feature/ação"})
    assert "Created branch feature/ação" in r.text
    assert run(repo_path, "branch", "--show-current") == "feature/ação"
    assert "data-prompt-url" in client.get("/partials/status").text


def test_create_branch_at_commit_and_bad_name(client, repo_path):
    from .conftest import run
    sha = run(repo_path, "rev-parse", "HEAD")
    r = client.post("/actions/branch/create", data={"name": "bad name", "start": sha})
    assert 'class="banner error"' in r.text and "not a valid branch name" in r.text
    r = client.post("/actions/branch/create", data={"name": "good", "start": sha})
    assert "Created branch good" in r.text


def test_delete_unmerged_shows_delete_anyway_button(client, repo_path):
    from .conftest import run
    run(repo_path, "checkout", "-q", "-b", "wip")
    run(repo_path, "commit", "-q", "--allow-empty", "-m", "wip")
    run(repo_path, "checkout", "-q", "main")
    r = client.post("/actions/branch/delete", data={"name": "wip"})
    assert "Delete anyway" in r.text and "&quot;force&quot;: &quot;1&quot;" in r.text
    r = client.post("/actions/branch/delete", data={"name": "wip", "force": "1"})
    assert "Deleted branch wip" in r.text


def test_conflict_box_and_abort(client, repo_path):
    from .conftest import run
    run(repo_path, "checkout", "-q", "--", "README.md")
    run(repo_path, "checkout", "-q", "-b", "feature")
    (repo_path / "README.md").write_text("feature\n")
    run(repo_path, "commit", "-q", "-am", "feature")
    run(repo_path, "checkout", "-q", "main")
    (repo_path / "README.md").write_text("main\n")
    run(repo_path, "commit", "-q", "-am", "main")
    r = client.post("/actions/branch/merge", data={"name": "feature"})
    assert 'class="operation"' in r.text and "Merge in progress" in r.text
    assert "Mark resolved and commit" in r.text and "Abort merge" in r.text
    r = client.post("/actions/operation/abort")
    assert "Merge aborted" in r.text and 'class="operation"' not in r.text


def test_reset_and_revert_endpoints(client, repo_path):
    from .conftest import run
    first = run(repo_path, "rev-parse", "HEAD")
    run(repo_path, "commit", "-q", "--allow-empty", "-m", "second")
    r = client.post("/actions/commit/reset", data={"sha": first, "mode": "soft"})
    assert f"Reset main to {first[:7]} (soft)" in r.text
    r = client.post("/actions/commit/reset", data={"sha": first, "mode": "bogus"})
    assert "Unknown reset mode" in r.text


def test_graph_rows_have_menu_data(client):
    html = client.get("/partials/graph").text
    assert 'data-merge="0"' in html and 'data-ref-kind="head"' in html


def test_index_has_context_menu(client):
    assert 'id="ctx-menu"' in client.get("/").text


# --- milestone 6: stash ---------------------------------------------------------

STASH_ACTIONS = ["/actions/stash/save", "/actions/stash/apply", "/actions/stash/pop", "/actions/stash/drop"]


@pytest.mark.parametrize("path", STASH_ACTIONS)
def test_stash_actions_protected(anon, path):
    assert anon.post(path).status_code == 403


@pytest.mark.parametrize("path", STASH_ACTIONS)
def test_stash_actions_reject_get(client, path):
    assert client.get(path).status_code == 405


def test_stash_flow_through_server(client, repo_path):
    html = client.get("/partials/status").text
    assert "Stash changes" in html and 'data-prompt-url="/actions/stash/save"' in html
    r = client.post("/actions/stash/save", data={"message": "trabalho em <progresso>"})
    assert "Stashed your changes" in r.text
    assert "&lt;progresso&gt;" in r.text and "<progresso>" not in r.text
    assert "Working tree clean" in r.text and "Stash changes" not in r.text  # nothing left to stash
    data = client.get("/api/status").json()
    assert data["clean"]

    from gitlane import stash as stash_mod
    from gitlane.git_ops import open_repo
    repo = open_repo(repo_path)
    [st] = stash_mod.list_stashes(repo)
    repo.close()

    detail = client.get(f"/partials/stash?index=0&sha={st.sha}").text
    assert "stash@{0}" in detail and "README.md" in detail and "untracked" in detail
    diff = client.get("/partials/stash-diff", params={"index": 0, "sha": st.sha, "path": "README.md"}).text
    assert 'class="add"' in diff and "stash@{0}" in diff

    r = client.post("/actions/stash/pop", data={"index": "0", "sha": st.sha})
    assert "Applied and removed stash@{0}" in r.text
    assert client.get("/api/status").json()["clean"] is False


def test_stash_drop_has_confirm(client, repo_path):
    client.post("/actions/stash/save", data={"message": "x"})
    html = client.get("/partials/status").text
    assert "/actions/stash/drop" in html and "Its changes will be lost" in html


def test_stale_stash_detail(client):
    html = client.get(f"/partials/stash?index=0&sha={'a' * 40}").text
    assert 'class="error"' in html and "changed" in html
