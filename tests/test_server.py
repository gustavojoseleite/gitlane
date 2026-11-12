from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from gitlane.server import COOKIE_NAME, REPO_HEADER, create_app
from gitlane.workspace import repo_id

WINDOWS = os.name == "nt"
TOKEN = "secret-token-for-tests"
PORT = 54321
BASE = f"http://127.0.0.1:{PORT}"


def make_client(path, follow_redirects=False):
    """A client for one repo tab: every request names the repo, like the page does."""
    return TestClient(create_app(path, TOKEN, PORT), base_url=BASE, follow_redirects=follow_redirects,
                      headers={REPO_HEADER: repo_id(path.resolve())})


@pytest.fixture
def repo_path(make_repo):
    path = make_repo()
    (path / "README.md").write_text("changed <b>bold</b>\n")
    if not WINDOWS:  # Windows doesn't allow < and > in file names
        (path / "<script>.txt").write_text("x")
    return path


@pytest.fixture
def anon(repo_path):
    """Client without the token cookie."""
    return make_client(repo_path)


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


@pytest.mark.skipif(WINDOWS, reason="needs a file name with < and >")
def test_api_status(client, repo_path):
    data = client.get("/api/status").json()
    assert data["branch"] == "main"
    assert data["name"] == repo_path.name
    assert data["clean"] is False
    assert [f["path"] for f in data["unstaged"]] == ["README.md"]
    assert [f["path"] for f in data["untracked"]] == ["<script>.txt"]


@pytest.mark.skipif(WINDOWS, reason="needs a file name with < and >")
def test_partial_status_escapes_html(client):
    html = client.get("/partials/status").text
    assert "main" in html and "Initial commit" in html
    assert "&lt;script&gt;.txt" in html
    assert "<script>" not in html


def test_partial_status_clean(make_repo):
    path = make_repo("clean")
    c = make_client(path)
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
    assert "gitlane:changed" in r.headers["hx-trigger-after-swap"]
    assert [f["path"] for f in client.get("/api/status").json()["staged"]] == ["README.md"]

    r = client.post("/actions/commit", data={"message": "Change readme"})
    assert "Committed" in r.text
    assert "gitlane:committed" in r.headers["hx-trigger-after-swap"]
    data = client.get("/api/status").json()
    assert data["head_summary"] == "Change readme" and data["staged"] == []


def test_commit_error_shown_in_banner(client):
    r = client.post("/actions/commit", data={"message": "nothing staged"})
    assert r.status_code == 200
    assert 'class="banner error"' in r.text and "Nothing staged" in r.text
    assert "gitlane:committed" not in r.headers.get("hx-trigger-after-swap", "")


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
    c = make_client(path)
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
    monkeypatch.setattr(graph.graph_page, "__defaults__", (0, 2, frozenset()))
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
    c = make_client(path)
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
    c = make_client(path, follow_redirects=True)
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

STASH_ACTIONS = ["/actions/stash/save", "/actions/stash/apply", "/actions/stash/pop", "/actions/stash/drop",
                 "/actions/stash/pop-onto", "/actions/stash/branch"]


@pytest.mark.parametrize("path", STASH_ACTIONS)
def test_stash_actions_protected(anon, path):
    assert anon.post(path).status_code == 403


@pytest.mark.parametrize("path", STASH_ACTIONS)
def test_stash_actions_reject_get(client, path):
    assert client.get(path).status_code == 405


def test_stash_flow_through_server(client, repo_path):
    (repo_path / "novo.txt").write_text("untracked")  # works on every OS, unlike <script>.txt
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


def test_stash_dialog_lists_files_and_saves_chosen_ones(client, repo_path):
    import json

    from gitlane import server
    from gitlane.git_ops import get_status, open_repo
    (repo_path / "novo.txt").write_text("untracked")
    html = client.get("/partials/status").text
    assert "data-prompt-files=" in html
    repo = open_repo(repo_path)
    try:
        files = server._stashable_files(get_status(repo))
    finally:
        repo.close()
    by_path = {f["path"]: f for f in files}
    assert by_path["README.md"]["untracked"] is False and by_path["novo.txt"]["untracked"] is True
    r = client.post("/actions/stash/save", data={"message": "", "paths": json.dumps(["novo.txt"])})
    assert "Stashed novo.txt." in r.text
    assert not (repo_path / "novo.txt").exists()
    assert (repo_path / "README.md").read_text().startswith("changed")


@pytest.mark.parametrize("paths", ["not json", '{"a": 1}', "[1, 2]"])
def test_stash_paths_must_be_a_list_of_names(client, paths):
    r = client.post("/actions/stash/save", data={"paths": paths})
    assert "Invalid list of files" in r.text


def test_stash_pop_onto_and_new_branch_through_server(client, repo_path):
    from gitlane import stash as stash_mod
    from gitlane.git_ops import open_repo

    from .conftest import run
    run(repo_path, "branch", "feature")
    client.post("/actions/stash/save", data={"message": "one"})

    def top():
        repo = open_repo(repo_path)
        try:
            return stash_mod.list_stashes(repo)[0]
        finally:
            repo.close()

    st = top()
    detail = client.get(f"/partials/stash?index=0&sha={st.sha}").text
    assert "Pop onto another branch…" in detail and "Pop into a new branch…" in detail
    assert 'data-prompt-url="/actions/stash/branch"' in detail
    r = client.post("/actions/stash/pop-onto", data={"index": "0", "sha": st.sha, "branch": "feature"})
    assert "Switched to feature, applied stash@{0} (one)" in r.text
    assert run(repo_path, "branch", "--show-current") == "feature"

    client.post("/actions/stash/save", data={"message": "two"})
    st = top()
    r = client.post("/actions/stash/branch", data={"index": "0", "sha": st.sha, "name": "from-two"})
    assert "Created from-two" in r.text
    assert run(repo_path, "branch", "--show-current") == "from-two"


def test_copy_hash_buttons(client, repo_path):
    from .conftest import run
    sha = run(repo_path, "rev-parse", "HEAD")
    detail = client.get(f"/partials/commit?sha={sha}").text
    assert f'data-copy="{sha}"' in detail and "Copy commit hash" in detail
    graph = client.get("/partials/graph").text
    assert f'data-copy="{sha}"' in graph


def test_stale_stash_detail(client):
    html = client.get(f"/partials/stash?index=0&sha={'a' * 40}").text
    assert 'class="error"' in html and "changed" in html


# --- commit message display --------------------------------------------------------

def test_reflow_joins_hard_wrapped_paragraphs():
    from gitlane.server import reflow
    body = ("The cover image was cut on phones because the\nhero is almost square.\n\n"
            "- first item\n- second item\n\n"
            "    code stays\n    as is\n\n"
            "Co-Authored-By: A <a@x>\nSigned-off-by: B <b@x>")
    assert reflow(body) == (
        "The cover image was cut on phones because the hero is almost square.\n\n"
        "- first item\n- second item\n\n"
        "    code stays\n    as is\n\n"
        "Co-Authored-By: A <a@x>\nSigned-off-by: B <b@x>")


def test_avatar_initials_and_stable_color():
    from gitlane.server import _avatar
    assert ">GL<" in _avatar("Gustavo Leite", "g@x") and ">A<" in _avatar("alice", "a@x")
    assert ">?<" in _avatar("", "") and _avatar("X Y", "same@x") == _avatar("X Y", "same@x")
    assert "&lt;" in _avatar("<b> c", "x") or ">C<" in _avatar("<b> c", "x")



def test_delete_remote_endpoint_and_button(make_repo, tmp_path):
    from .conftest import run
    path = make_repo()
    run(tmp_path, "init", "-q", "--bare", "server.git")
    run(path, "remote", "add", "origin", str(tmp_path / "server.git"))
    run(path, "push", "-q", "-u", "origin", "main")
    run(path, "push", "-q", "origin", "main:tmp")
    run(path, "fetch", "-q")
    c = make_client(path)
    c.get(f"/?token={TOKEN}")
    html = c.get("/partials/status").text
    assert 'hx-post="/actions/branch/delete-remote"' in html and "Delete tmp on origin?" in html
    assert 'data-staged="0"' in html
    r = c.post("/actions/branch/delete-remote", data={"name": "origin/tmp"})
    assert "Deleted tmp on origin." in r.text
    assert c.get("/actions/branch/delete-remote").status_code == 405


def test_long_lists_start_minimized(make_repo):
    path = make_repo()
    for i in range(16):
        (path / f"u{i}.txt").write_text("x")
    (path / "README.md").write_text("changed\n")
    c = make_client(path)
    c.get(f"/?token={TOKEN}")
    html = c.get("/partials/status").text
    assert 'data-section="untracked" data-auto-collapse="1"' in html  # 16 > 15
    assert 'data-section="unstaged" data-auto-collapse="0"' in html  # changes are never auto-hidden
    assert 'data-section="branches" data-auto-collapse="0"' in html
    assert html.count('class="sec-toggle"') == 3


def test_favicon_served(client):
    r = client.get("/static/gitlane.svg")
    assert r.status_code == 200 and r.text.startswith("<svg")
    assert 'rel="icon"' in client.get("/").text


def test_branch_location_icons(make_repo, tmp_path):
    from .conftest import run
    path = make_repo()
    run(tmp_path, "init", "-q", "--bare", "server.git")
    run(path, "remote", "add", "origin", str(tmp_path / "server.git"))
    run(path, "push", "-q", "-u", "origin", "main")
    run(path, "push", "-q", "origin", "main:only-remote")
    run(path, "branch", "only-local")
    run(path, "fetch", "-q")
    c = make_client(path)
    c.get(f"/?token={TOKEN}")
    html = c.get("/partials/status").text
    assert 'title="on your computer and on origin (origin/main)"' in html
    assert 'title="only on your computer (not pushed yet)"' in html
    assert 'title="only on the remote (click to get a local copy)"' in html
    assert 'title="on the remote and on your computer"' in html  # origin/main is tracked
    graph_html = c.get("/partials/graph").text
    assert 'data-ref-name="main" data-ref-remote="origin/main"' in graph_html
    assert "main — on your computer and on origin (same commit)" in graph_html
    assert 'data-ref-name="origin/main"' not in graph_html  # folded into "main"
    assert "only-remote — only on the remote" in graph_html.replace("origin/only-remote", "only-remote")


def test_tag_endpoints_and_sidebar(make_repo, tmp_path):
    from .conftest import run
    path = make_repo()
    sha = run(path, "rev-parse", "HEAD")
    run(tmp_path, "init", "-q", "--bare", "server.git")
    run(path, "remote", "add", "origin", str(tmp_path / "server.git"))
    c = make_client(path)
    c.get(f"/?token={TOKEN}")
    assert 'data-section="tags"' not in c.get("/partials/status").text  # no tags: no section
    r = c.post("/actions/tag/create", data={"name": "v1.0", "sha": sha, "message": "First <release>"})
    assert f"Created tag v1.0 at {sha[:7]}." in r.text
    assert 'data-section="tags"' in r.text and f'data-ref-sha="{sha}"' in r.text
    assert "First &lt;release&gt;" in r.text  # message escaped in the tooltip
    assert "Delete the tag v1.0?" in r.text  # delete asks first
    assert "already exists" in c.post("/actions/tag/create", data={"name": "v1.0", "sha": sha}).text
    assert "not a valid tag name" in c.post("/actions/tag/create", data={"name": "a b", "sha": sha}).text
    assert "Pushed tag v1.0 to origin." in c.post("/actions/tag/push", data={"name": "v1.0"}).text
    assert run(tmp_path / "server.git", "tag") == "v1.0"
    r = c.post("/actions/tag/delete-remote", data={"name": "v1.0"})
    assert "Deleted tag v1.0 on origin." in r.text
    assert "Deleted tag v1.0" in c.post("/actions/tag/delete", data={"name": "v1.0"}).text
    assert run(path, "tag") == ""
    assert c.get("/actions/tag/delete").status_code == 405


def test_tag_label_in_graph(make_repo):
    from .conftest import run
    path = make_repo()
    run(path, "tag", "v9")
    c = make_client(path)
    c.get(f"/?token={TOKEN}")
    html = c.get("/partials/graph").text
    assert 'data-ref-kind="tag" data-ref-name="v9"' in html and "🏷" not in html


def test_hide_branch_endpoints(make_repo, tmp_path):
    from .conftest import run
    path = make_repo()
    run(path, "switch", "-q", "-c", "feature")
    run(path, "commit", "-q", "--allow-empty", "-m", "secret work")
    run(path, "switch", "-q", "main")
    c = make_client(path)
    c.get(f"/?token={TOKEN}")
    v1 = c.get("/api/refs-version").json()["version"]
    assert "secret work" in c.get("/partials/graph").text
    r = c.post("/actions/branch/hide", data={"kind": "branch", "name": "feature"})
    assert "Hid feature in the graph" in r.text
    assert 'class="branch-item is-hidden"' in r.text and 'data-hidden="1"' in r.text
    assert "Show 1 hidden" in r.text
    assert "secret work" not in c.get("/partials/graph").text
    assert c.get("/api/refs-version").json()["version"] != v1  # the graph reloads
    assert "current branch" in c.post("/actions/branch/hide", data={"kind": "branch", "name": "main"}).text
    r = c.post("/actions/branch/show-all")
    assert "All branches are shown" in r.text and "is-hidden" not in r.text
    assert "secret work" in c.get("/partials/graph").text
    assert c.get("/actions/branch/hide").status_code == 405


def test_choose_folder_endpoint(make_repo, tmp_path, monkeypatch):
    from gitlane import folder_dialog
    path, other = make_repo(), make_repo("other")
    c = make_client(path)
    c.get(f"/?token={TOKEN}")
    monkeypatch.setattr(folder_dialog, "available", lambda: True)
    html = c.get("/partials/picker").text
    assert "data-choose-folder" in html and 'id="open-path"' not in html
    monkeypatch.setattr(folder_dialog, "available", lambda: False)
    html = c.get("/partials/picker").text
    assert "data-choose-folder" not in html and 'id="open-path"' in html  # fallback

    starts = []
    def pick(result):
        def choose(start=None):
            starts.append(start)
            if isinstance(result, Exception):
                raise result
            return result
        monkeypatch.setattr(folder_dialog, "choose_folder", choose)

    pick(other)
    data = c.post("/api/workspace/choose", data={"start": str(tmp_path)}).json()
    assert data["tab"]["name"] == "other" and starts[-1] == str(tmp_path)
    pick(None)
    assert c.post("/api/workspace/choose").json() == {"cancelled": True}
    pick(tmp_path)  # not a repo
    r = c.post("/api/workspace/choose")
    assert r.status_code == 400 and "Not a Git repository" in r.json()["error"]
    pick(folder_dialog.DialogBusy("A folder window is already open."))
    assert c.post("/api/workspace/choose").status_code == 409
    assert c.get("/api/workspace/choose").status_code == 405


def test_submodule_in_sidebar_and_diff(tmp_path):
    from .conftest import run
    run(tmp_path, "config", "--global", "protocol.file.allow", "always")
    lib, app = tmp_path / "lib", tmp_path / "app"
    for d in (lib, app):
        d.mkdir()
        run(d, "init", "-q")
        run(d, "commit", "-q", "--allow-empty", "-m", f"{d.name} 1")
    run(lib, "commit", "-q", "--allow-empty", "-m", "lib <2>")
    run(app, "submodule", "add", "-q", str(lib), "lib")
    run(app, "commit", "-q", "-m", "add lib")
    run(app / "lib", "checkout", "-q", "HEAD~1")
    c = make_client(app)
    c.get(f"/?token={TOKEN}")
    html = c.get("/partials/status").text
    assert 'data-section="submodules"' in html and "on another commit" in html
    assert "submodule · new commits" in html and "Back to the recorded commit" in html
    assert f'data-open-repo="{app / "lib"}"' in html
    diff = c.get("/partials/diff?path=lib&staged=0").text
    assert "Submodule" in diff and "lib &lt;2&gt;" in diff and "Open lib in a new tab" in diff
    r = c.post("/actions/submodule/update", data={"path": "lib"})
    assert "lib is on the commit this project records." in r.text and "on another commit" not in r.text


# --- slow actions --------------------------------------------------------------

def test_slow_action_does_not_block_the_server(client, monkeypatch):
    """A stuck pull must not freeze the page: reads still answer, and a second
    action in the same repo is refused right away instead of waiting."""
    import threading

    from gitlane import git_ops

    started, release = threading.Event(), threading.Event()

    def slow_fetch(repo):
        started.set()
        assert release.wait(10), "test never released the slow action"
        return "Fetched."

    monkeypatch.setattr(git_ops, "fetch", slow_fetch)
    with client:  # one event loop shared by both threads, like the real server
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("r", client.post("/actions/fetch")))
        worker.start()
        try:
            assert started.wait(10)
            status = client.get("/partials/status")
            assert status.status_code == 200
            busy = client.post("/actions/fetch")
            assert busy.status_code == 200
            assert "Another action is still running" in busy.text
        finally:
            release.set()
            worker.join(10)
        assert "Fetched." in result["r"].text
        # Once the slow one finished, actions work again.
        assert "Another action" not in client.post("/actions/stage-all").text


def test_stash_untracked_box_through_server(client, repo_path):
    (repo_path / "novo.txt").write_text("untracked")
    html = client.get("/partials/status").text
    assert 'data-prompt-check-field="untracked"' in html and "Include untracked files" in html
    r = client.post("/actions/stash/save", data={"message": "", "untracked": "0"})
    assert "Untracked files were kept" in r.text
    assert (repo_path / "novo.txt").exists() and (repo_path / "README.md").read_text() == "hello\n"
    # Nothing but untracked files left: no box to tick, it's the only thing to stash.
    assert "Stash changes" in r.text


def test_submodule_stash_through_server(tmp_path):
    from .conftest import run
    run(tmp_path, "config", "--global", "protocol.file.allow", "always")
    lib, app = tmp_path / "lib", tmp_path / "app"
    for d in (lib, app):
        d.mkdir()
        run(d, "init", "-q")
    (lib / "lib.txt").write_text("v1\n")
    run(lib, "add", "lib.txt")
    run(lib, "commit", "-q", "-m", "lib")
    run(app, "commit", "-q", "--allow-empty", "-m", "init")
    run(app, "submodule", "add", "-q", str(lib), "vendor/lib")
    run(app, "commit", "-q", "-m", "add lib")
    (app / "vendor/lib/lib.txt").write_text("v2\n")
    c = make_client(app)
    c.get(f"/?token={TOKEN}")
    html = c.get("/partials/status").text
    assert 'data-prompt-url="/actions/submodule/stash"' in html
    assert "&quot;path&quot;: &quot;vendor/lib&quot;" in html
    r = c.post("/actions/submodule/stash", data={"path": "vendor/lib", "message": "wip"})
    assert "Stashed the changes inside vendor/lib" in r.text
    assert (app / "vendor/lib/lib.txt").read_text() == "v1\n"
    assert "Working tree clean" in r.text
    assert c.post("/actions/submodule/stash", data={"path": "../x"}).status_code == 200
    assert anon_post_forbidden(app, "/actions/submodule/stash")


def anon_post_forbidden(path, url) -> bool:
    return make_client(path).post(url, data={"path": "vendor/lib"}).status_code == 403
