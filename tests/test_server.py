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
