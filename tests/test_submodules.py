from __future__ import annotations

from pathlib import Path

import pytest

from gitlane import graph, submodules
from gitlane.git_ops import GitError, diff_file, discard, get_status, is_submodule, open_repo

from .conftest import run


@pytest.fixture
def project(tmp_path):
    """app/ with the submodule vendor/lib (lib has commits "lib 1" and "lib 2")."""
    run(tmp_path, "config", "--global", "protocol.file.allow", "always")  # local clones in tests
    lib = tmp_path / "lib"
    lib.mkdir()
    run(lib, "init", "-q")
    for msg in ("lib 1", "lib 2"):
        run(lib, "commit", "-q", "--allow-empty", "-m", msg)
    app = tmp_path / "app"
    app.mkdir()
    run(app, "init", "-q")
    run(app, "commit", "-q", "--allow-empty", "-m", "init")
    run(app, "submodule", "add", "-q", str(lib), "vendor/lib")
    run(app, "commit", "-q", "-m", "add lib")
    return app


@pytest.fixture
def repo(project):
    r = open_repo(project)
    yield r
    r.close()


def sub_dir(repo) -> Path:
    return Path(repo.working_tree_dir) / "vendor" / "lib"


def test_list_up_to_date(repo):
    [sm] = submodules.list_submodules(repo)
    assert (sm.path, sm.state, sm.ready) == ("vendor/lib", "up to date", True)
    assert len(sm.sha) >= 40


def test_no_submodules(make_repo):
    r = open_repo(make_repo("plain"))
    try:
        assert submodules.list_submodules(r) == []
        with pytest.raises(GitError, match="no submodules"):
            submodules.update(r)
        assert not is_submodule(r, "README.md")
    finally:
        r.close()


def test_status_and_diff_of_moved_submodule(repo):
    run(sub_dir(repo), "checkout", "-q", "HEAD~1")
    (sub_dir(repo) / "new.txt").write_text("x")
    [change] = get_status(repo).unstaged
    assert change.path == "vendor/lib" and change.submodule == "new commits, untracked files inside"
    assert is_submodule(repo, "vendor/lib")
    d = diff_file(repo, "vendor/lib")
    assert d.submodule.note == "rewind" and d.submodule.commits == [("-", "lib 2")]
    assert d.submodule.untracked and not d.submodule.modified and not d.empty
    assert [sm.state for sm in submodules.list_submodules(repo)] == ["on another commit"]


def test_staged_submodule(repo):
    run(sub_dir(repo), "checkout", "-q", "HEAD~1")
    run(Path(repo.working_tree_dir), "add", "vendor/lib")
    [staged] = get_status(repo).staged
    assert staged.submodule == "commit changed"
    d = diff_file(repo, "vendor/lib", staged=True)
    assert d.submodule.old and d.submodule.new and d.submodule.old != d.submodule.new


def test_changed_files_inside(repo):
    (sub_dir(repo) / "tracked.txt").write_text("a")
    run(sub_dir(repo), "add", "tracked.txt")
    [change] = get_status(repo).unstaged
    assert change.submodule == "changed files inside"
    assert diff_file(repo, "vendor/lib").submodule.modified


def test_commit_diff_of_new_submodule(repo):
    sha = run(Path(repo.working_tree_dir), "rev-parse", "HEAD")
    d = graph.commit_file_diff(repo, sha, "vendor/lib")
    assert d.submodule.note == "new submodule" and d.submodule.old is None and d.submodule.new


def test_discard_moves_back_to_recorded_commit(repo):
    recorded = run(sub_dir(repo), "rev-parse", "HEAD")
    run(sub_dir(repo), "checkout", "-q", "HEAD~1")
    discard(repo, "vendor/lib")
    assert run(sub_dir(repo), "rev-parse", "HEAD") == recorded
    assert get_status(repo).unstaged == []
    assert "lib 2" in run(sub_dir(repo), "log", "--all", "--format=%s")  # nothing lost


def test_discard_never_touches_files_inside(repo):
    (sub_dir(repo) / "mine.txt").write_text("keep me")
    with pytest.raises(GitError, match="separate repository") as e:
        discard(repo, "vendor/lib")
    assert e.value.action["open"].endswith("lib") and "own tab" in e.value.hint
    assert (sub_dir(repo) / "mine.txt").read_text() == "keep me"


def test_set_up_after_clone(project, tmp_path):
    run(tmp_path, "clone", "-q", str(project), "copy")
    r = open_repo(tmp_path / "copy")
    try:
        [sm] = submodules.list_submodules(r)
        assert (sm.state, sm.ready) == ("not set up", False)
        assert submodules.update(r, "vendor/lib") == "vendor/lib is on the commit this project records."
        assert submodules.list_submodules(r)[0].state == "up to date"
        assert (tmp_path / "copy" / "vendor" / "lib" / ".git").exists()
        assert "All submodules" in submodules.update(r)
        with pytest.raises(GitError, match="not found"):
            submodules.update(r, "nope")
    finally:
        r.close()
