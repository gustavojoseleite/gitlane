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
