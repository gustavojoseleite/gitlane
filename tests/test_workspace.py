"""Milestone 7: tabs, recent repos, folder browser — module and HTTP API."""

from __future__ import annotations

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from gitlane import workspace
from gitlane.git_ops import NotAGitRepo
from gitlane.server import REPO_HEADER, create_app
from gitlane.workspace import Workspace, repo_id

from .conftest import run
from .test_server import BASE, PORT, TOKEN


@pytest.fixture
def state(tmp_path):
    return tmp_path / "state" / "state.json"


def test_default_state_file_is_in_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert Workspace().state_file == tmp_path / "cfg" / "gitlane" / "state.json"


# --- Workspace -------------------------------------------------------------------

def test_open_uses_repo_root_and_is_idempotent(make_repo, state):
    path = make_repo()
    (path / "sub" / "deeper").mkdir(parents=True)
    ws = Workspace(state)
    a = ws.open(path / "sub" / "deeper")
    b = ws.open(path)
    assert a.id == b.id == repo_id(path.resolve()) and a.path == path.resolve()
    assert len(ws.tabs) == 1 and ws.active == a.id


@pytest.mark.parametrize("bad", ["", "   ", "relative/path"])
def test_open_rejects_empty_and_relative(state, bad):
    with pytest.raises(NotAGitRepo):
        Workspace(state).open(bad)


def test_open_rejects_non_repo_and_missing(tmp_path, state):
    with pytest.raises(NotAGitRepo, match="Not a Git repository"):
        Workspace(state).open(tmp_path)
    with pytest.raises(NotAGitRepo, match="does not exist"):
        Workspace(state).open(tmp_path / "nope")


def test_open_expands_home(make_repo, state, monkeypatch, tmp_path):
    path = make_repo()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Workspace(state).open("~/repo").path == path.resolve()


def test_tabs_and_recent_survive_restart(make_repo, state):
    a, b, c = make_repo("a"), make_repo("b"), make_repo("c")
    ws = Workspace(state)
    for p in (a, b, c):
        ws.open(p)
    ws.activate(repo_id(b.resolve()))
    ws.close(repo_id(c.resolve()))
    again = Workspace(state)
    assert [t.name for t in again.tabs] == ["a", "b"]
    assert again.active == repo_id(b.resolve())
    assert again.recent_repos() == [c.resolve()]  # closed, so offered as recent


def test_close_active_picks_neighbour(make_repo, state):
    ws = Workspace(state)
    tabs = [ws.open(make_repo(n)) for n in ("a", "b", "c")]
    ws.activate(tabs[1].id)
    ws.close(tabs[1].id)
    assert ws.active == tabs[2].id
    ws.close(tabs[2].id)
    assert ws.active == tabs[0].id
    ws.close(tabs[0].id)
    assert ws.active is None and ws.tabs == []
    ws.close("unknown")  # no error


def test_closing_never_touches_the_repo(make_repo, state):
    path = make_repo()
    ws = Workspace(state)
    ws.close(ws.open(path).id)
    assert (path / ".git").is_dir() and (path / "README.md").exists()


def test_deleted_repos_are_dropped(make_repo, state):
    a, b = make_repo("a"), make_repo("b")
    ws = Workspace(state)
    ws.open(a)
    ws.open(b)
    ws.close(repo_id(b.resolve()))
    shutil.rmtree(a)
    shutil.rmtree(b)
    again = Workspace(state)
    assert again.tabs == [] and again.active is None and again.recent_repos() == []


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '{"open": [42, null], "recent": "x"}'])
def test_broken_state_file_is_ignored(state, content):
    state.parent.mkdir(parents=True)
    state.write_text(content)
    ws = Workspace(state)
    assert ws.tabs == []


def test_recent_is_capped_and_most_recent_first(make_repo, state):
    ws = Workspace(state)
    paths = [make_repo(f"r{i}") for i in range(workspace.MAX_RECENT + 3)]
    for p in paths:
        ws.open(p)
    assert len(ws.recent) == workspace.MAX_RECENT
    assert ws.recent[0] == paths[-1].resolve()


def test_unwritable_state_does_not_break(make_repo, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ws = Workspace(blocker / "state.json")  # parent is a file: can't save
    assert ws.open(make_repo()).name == "repo"


# --- folder browser --------------------------------------------------------------

def test_browse_lists_folders_and_marks_repos(make_repo, tmp_path):
    make_repo("proj")
    (tmp_path / "plain").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")
    listing = workspace.browse(tmp_path)
    assert [(f.name, f.is_repo) for f in listing.folders] == [("plain", False), ("proj", True)]
    assert listing.parent == tmp_path.parent and not listing.is_repo


def test_browse_defaults_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert workspace.browse(None).path == tmp_path.resolve()


def test_browse_root_has_no_parent():
    assert workspace.browse("/").parent is None


@pytest.mark.parametrize("bad", ["relative", "/definitely/not/here"])
def test_browse_bad_paths(bad):
    with pytest.raises(NotAGitRepo):
        workspace.browse(bad)


# --- HTTP API ----------------------------------------------------------------------

def client_for(path=None):
    c = TestClient(create_app(path, TOKEN, PORT), base_url=BASE, follow_redirects=False)
    assert c.get(f"/?token={TOKEN}").status_code == 303
    return c


def test_requests_without_or_with_unknown_repo_are_refused(make_repo):
    c = client_for(make_repo())
    assert c.get("/api/status").status_code == 409
    r = c.post("/actions/stage-all", headers={REPO_HEADER: "0123456789ab"})
    assert r.status_code == 409 and "isn't open" in r.text


def test_two_tabs_see_their_own_repo(make_repo):
    a, b = make_repo("a"), make_repo("b")
    (b / "only-in-b.txt").write_text("x")
    c = client_for(a)
    r = c.post("/api/workspace/open", data={"path": str(b)})
    assert r.status_code == 200
    data = r.json()
    assert [t["name"] for t in data["tabs"]] == ["a", "b"] and data["active"] == data["tab"]["id"]
    ida, idb = (t["id"] for t in data["tabs"])
    assert c.get("/api/status", headers={REPO_HEADER: ida}).json()["untracked"] == []
    assert c.get("/api/status", headers={REPO_HEADER: idb}).json()["untracked"][0]["path"] == "only-in-b.txt"
    # an action only changes the repo it names
    c.post("/actions/stage-all", headers={REPO_HEADER: idb})
    assert run(b, "diff", "--cached", "--name-only") == "only-in-b.txt"
    assert run(a, "status", "--porcelain") == ""


def test_open_error_is_readable(make_repo, tmp_path):
    c = client_for(make_repo())
    r = c.post("/api/workspace/open", data={"path": str(tmp_path / "nope")})
    assert r.status_code == 400 and "does not exist" in r.json()["error"]


def test_close_tab_then_requests_refused(make_repo):
    path = make_repo()
    c = client_for(path)
    rid = repo_id(path.resolve())
    assert c.post("/api/workspace/close", data={"id": rid}).json() == {"tabs": [], "active": None}
    assert c.get("/api/status", headers={REPO_HEADER: rid}).status_code == 409


def test_activate_is_remembered(make_repo):
    a, b = make_repo("a"), make_repo("b")
    c = client_for(a)
    c.post("/api/workspace/open", data={"path": str(b)})
    c.post("/api/workspace/activate", data={"id": repo_id(a.resolve())})
    assert client_for(None).get("/api/workspace").json()["active"] == repo_id(a.resolve())


def test_start_without_repo_reopens_saved_tabs(make_repo):
    path = make_repo()
    client_for(path)
    data = client_for(None).get("/api/workspace").json()
    assert [t["path"] for t in data["tabs"]] == [str(path.resolve())]


def test_start_without_anything_has_no_tabs():
    assert client_for(None).get("/api/workspace").json() == {"tabs": [], "active": None}


def test_workspace_api_needs_token_and_same_origin(make_repo, tmp_path):
    anon = TestClient(create_app(make_repo(), TOKEN, PORT), base_url=BASE)
    assert anon.get("/api/workspace").status_code == 403
    assert anon.post("/api/workspace/open", data={"path": str(tmp_path)}).status_code == 403
    assert anon.get("/partials/picker").status_code == 403
    c = client_for(make_repo("other"))
    r = c.post("/api/workspace/open", data={"path": str(tmp_path)}, headers={"origin": "http://evil.com"})
    assert r.status_code == 403
    assert c.get("/api/workspace/open").status_code == 405  # GET can't change state


def test_blob_urls_name_the_repo(make_repo):
    path = make_repo()
    (path / "new.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    c = client_for(path)
    rid = repo_id(path.resolve())
    html = c.get("/partials/diff?path=new.png", headers={REPO_HEADER: rid}).text
    assert f"&amp;repo={rid}" in html
    assert c.get(f"/blob?rev=WORKTREE&path=new.png&repo={rid}").status_code == 200
    assert c.get("/blob?rev=WORKTREE&path=new.png").status_code == 409


def test_toolbar_sent_with_status(make_repo):
    path = make_repo()
    (path / "README.md").write_text("changed\n")
    c = client_for(path)
    html = c.get("/partials/status", headers={REPO_HEADER: repo_id(path.resolve())}).text
    toolbar = html[html.index('id="toolbar"'):html.index("</div></div>")]
    assert 'hx-swap-oob="true"' in toolbar and 'data-branch="main"' in toolbar
    for label in ("Fetch", "Pull", "Push", "New branch", "Stash changes"):
        assert label in toolbar


def test_picker_partial(make_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = make_repo("proj")
    (tmp_path / "plain").mkdir()
    (tmp_path / "<b>").mkdir()
    gone = make_repo("closed")
    c = client_for(proj)
    rid = repo_id(gone.resolve())
    c.post("/api/workspace/open", data={"path": str(gone)})
    c.post("/api/workspace/close", data={"id": rid})
    html = c.get("/partials/picker").text
    assert "Open a repository" in html
    recent = html[html.index('class="files recent"'):html.index('class="files browser"')]
    assert f'data-open-path="{gone.resolve()}"' in recent and str(proj) not in recent  # open tabs aren't "recent"
    assert f'data-open-path="{proj}"' in html  # marked as a repo in the browser
    assert "plain" in html and "&lt;b&gt;" in html and "<b>" not in html
    sub = c.get("/partials/picker", params={"path": str(tmp_path / "plain")}).text
    assert "No folders here." in sub
    bad = c.get("/partials/picker", params={"path": "/not/here"}).text
    assert 'class="banner error"' in bad and "does not exist" in bad


def test_state_saved_in_the_test_home_not_the_real_one(make_repo):
    client_for(make_repo())
    saved = json.loads((workspace.Path.home() / ".config" / "gitlane" / "state.json").read_text())
    assert len(saved["open"]) == 1
