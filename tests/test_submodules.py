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


# --- stash with submodules ---------------------------------------------------------------

from gitlane import stash  # noqa: E402


@pytest.fixture
def dirty_sub(repo):
    """vendor/lib has a tracked file; it is changed, and an untracked file is added."""
    sub = sub_dir(repo)
    (sub / "lib.txt").write_text("v1\n")
    run(sub, "add", "lib.txt")
    run(sub, "commit", "-q", "-m", "lib file")
    run(Path(repo.working_tree_dir), "commit", "-q", "-am", "use lib file")
    (sub / "lib.txt").write_text("v2\n")
    (sub / "notes.txt").write_text("mine")
    return sub


def test_stash_with_only_submodule_changes_says_why(repo, dirty_sub):
    with pytest.raises(GitError, match="only changes are inside vendor/lib"):
        stash.save(repo)
    assert stash.list_stashes(repo) == []
    assert (dirty_sub / "lib.txt").read_text() == "v2\n"


def test_stash_tells_what_stayed_in_the_submodule(repo, dirty_sub):
    (Path(repo.working_tree_dir) / "top.txt").write_text("x")
    msg = stash.save(repo)
    assert "Changes inside vendor/lib were not stashed" in msg
    assert "working tree is clean" not in msg
    assert len(stash.list_stashes(repo)) == 1


def test_stash_inside_submodule(repo, dirty_sub):
    msg = stash.save_in_submodule(repo, "vendor/lib", "wip lib")
    assert "Stashed the changes inside vendor/lib" in msg
    assert (dirty_sub / "lib.txt").read_text() == "v1\n" and not (dirty_sub / "notes.txt").exists()
    assert get_status(repo).clean
    assert stash.list_stashes(repo) == []  # the project's own list is untouched
    inner = open_repo(dirty_sub)
    try:
        [s] = stash.list_stashes(inner)
        assert s.title == "wip lib"
    finally:
        inner.close()


def test_stash_inside_submodule_can_keep_untracked(repo, dirty_sub):
    stash.save_in_submodule(repo, "vendor/lib", "", include_untracked=False)
    assert (dirty_sub / "lib.txt").read_text() == "v1\n"
    assert (dirty_sub / "notes.txt").read_text() == "mine"


def test_stash_inside_unknown_or_missing_submodule(repo, project, tmp_path):
    with pytest.raises(GitError, match="not found"):
        stash.save_in_submodule(repo, "nope")
    with pytest.raises(GitError):
        stash.save_in_submodule(repo, "../outside")
    clone = tmp_path / "clone"
    run(tmp_path, "clone", "-q", str(project), str(clone))  # submodule not set up
    r = open_repo(clone)
    try:
        with pytest.raises(GitError, match="not set up"):
            stash.save_in_submodule(r, "vendor/lib")
    finally:
        r.close()


# --- pull updates submodules ------------------------------------------------------------

class Team:
    """``project`` pushed to a bare remote, and a clone of it (with the submodule set up)."""

    def __init__(self, tmp_path: Path, project: Path):
        self.project = project
        self.lib = tmp_path / "lib"
        self.remote = tmp_path / "app.git"
        run(tmp_path, "init", "-q", "--bare", str(self.remote))
        run(project, "remote", "add", "origin", str(self.remote))
        run(project, "push", "-q", "-u", "origin", "main")
        self.clone = tmp_path / "clone"
        run(tmp_path, "clone", "-q", "--recurse-submodules", str(self.remote), str(self.clone))
        self.repo = open_repo(self.clone)

    def push_new_lib_commit(self, msg: str = "lib 3") -> str:
        """A teammate moves vendor/lib to a new commit and pushes the project."""
        run(self.lib, "commit", "-q", "--allow-empty", "-m", msg)
        inner = self.project / "vendor" / "lib"
        run(inner, "pull", "-q", "origin", "HEAD")
        sha = run(inner, "rev-parse", "HEAD")
        run(self.project, "commit", "-q", "-am", f"lib to {msg}")
        run(self.project, "push", "-q")
        return sha

    def lib_head(self) -> str:
        return run(self.clone / "vendor" / "lib", "rev-parse", "HEAD")


@pytest.fixture
def team(tmp_path, project):
    t = Team(tmp_path, project)
    yield t
    t.repo.close()


def test_pull_moves_submodule_to_new_recorded_commit(team):
    from gitlane.git_ops import pull

    sha = team.push_new_lib_commit()
    msg = pull(team.repo)
    assert "Pulled 1 new commit" in msg and "Updated submodule vendor/lib." in msg
    assert team.lib_head() == sha
    [sm] = submodules.list_submodules(team.repo)
    assert sm.state == "up to date"
    assert get_status(team.repo).clean


def test_pull_leaves_a_submodule_you_moved(team):
    from gitlane.git_ops import pull

    inner = team.clone / "vendor" / "lib"
    run(inner, "checkout", "-q", "HEAD~1")  # your own choice
    mine = team.lib_head()
    team.push_new_lib_commit()
    msg = pull(team.repo)
    assert "Left vendor/lib on the commit you chose" in msg
    assert team.lib_head() == mine


def test_pull_without_submodule_changes_says_nothing_about_them(team):
    from gitlane.git_ops import pull

    (team.project / "notes.txt").write_text("n")
    run(team.project, "add", "notes.txt")
    run(team.project, "commit", "-q", "-m", "notes")
    run(team.project, "push", "-q")
    msg = pull(team.repo)
    assert msg.startswith("Pulled 1 new commit") and "submodule" not in msg


def test_pull_sets_up_a_new_submodule(team, tmp_path):
    from gitlane.git_ops import pull

    other = tmp_path / "other"
    other.mkdir()
    run(other, "init", "-q")
    run(other, "commit", "-q", "--allow-empty", "-m", "other 1")
    run(team.project, "submodule", "add", "-q", str(other), "vendor/other")
    run(team.project, "commit", "-q", "-m", "add other")
    run(team.project, "push", "-q")
    msg = pull(team.repo)
    assert "Updated submodule vendor/other." in msg
    states = {s.path: s.state for s in submodules.list_submodules(team.repo)}
    assert states == {"vendor/lib": "up to date", "vendor/other": "up to date"}


def test_pull_keeps_a_submodule_you_never_set_up(team, tmp_path):
    from gitlane.git_ops import pull

    run(team.clone, "submodule", "deinit", "-q", "vendor/lib")
    team.push_new_lib_commit()
    msg = pull(team.repo)
    assert "Updated" not in msg
    [sm] = submodules.list_submodules(team.repo)
    assert sm.state == "not set up"


def test_pull_reports_a_failed_submodule_update(team):
    from gitlane.git_ops import pull

    (team.lib / "f.txt").write_text("from lib")
    run(team.lib, "add", "f.txt")
    team.push_new_lib_commit()
    # an untracked file inside the submodule that the new commit would overwrite
    (team.clone / "vendor" / "lib" / "f.txt").write_text("mine")
    with pytest.raises(GitError, match="The pull worked, but updating the submodules") as e:
        pull(team.repo)
    assert run(team.clone, "log", "-1", "--format=%s") == "lib to lib 3"
    assert (team.clone / "vendor" / "lib" / "f.txt").read_text() == "mine"  # never thrown away
    assert e.value.hint
