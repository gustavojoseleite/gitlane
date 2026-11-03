from __future__ import annotations

from pathlib import Path

import pytest

from gitlane import stash
from gitlane.git_ops import GitError, get_status, open_repo

from .conftest import run
from .test_blobs import PNG


@pytest.fixture
def repo(make_repo):
    r = open_repo(make_repo())
    yield r
    r.close()


def root(r) -> Path:
    return Path(r.working_tree_dir)


def make_changes(r):
    (root(r) / "README.md").write_text("changed\n")
    (root(r) / "new.txt").write_text("untracked")


# --- save & list -----------------------------------------------------------------

def test_save_includes_untracked_and_cleans_tree(repo):
    make_changes(repo)
    msg = stash.save(repo, "half-done login")
    assert "half-done login" in msg
    assert get_status(repo).clean
    [s] = stash.list_stashes(repo)
    assert s.index == 0 and s.ref == "stash@{0}"
    assert s.title == "half-done login" and s.branch == "main"


def test_save_without_message(repo):
    make_changes(repo)
    stash.save(repo, "  ")
    [s] = stash.list_stashes(repo)
    assert s.branch == "main" and s.message.startswith("WIP on main:")


def test_save_keeps_staged_changes_too(repo):
    (root(repo) / "README.md").write_text("staged\n")
    run(root(repo), "add", "README.md")
    stash.save(repo)
    assert get_status(repo).clean


def test_nothing_to_stash(repo):
    with pytest.raises(GitError, match="no changes"):
        stash.save(repo)


def test_stash_in_empty_repo(make_repo):
    r = open_repo(make_repo(commit=False))
    (root(r) / "a").write_text("a")
    with pytest.raises(GitError, match="first commit"):
        stash.save(r)
    r.close()


def test_list_order_newest_first(repo):
    for name in ("first", "second"):
        (root(repo) / "README.md").write_text(name)
        stash.save(repo, name)
    assert [s.title for s in stash.list_stashes(repo)] == ["second", "first"]


def test_empty_list(repo):
    assert stash.list_stashes(repo) == []


# --- apply / pop / drop -----------------------------------------------------------

def test_apply_keeps_stash(repo):
    make_changes(repo)
    stash.save(repo, "x")
    [s] = stash.list_stashes(repo)
    assert "kept" in stash.apply(repo, s.index, s.sha)
    assert (root(repo) / "new.txt").read_text() == "untracked"
    assert (root(repo) / "README.md").read_text() == "changed\n"
    assert len(stash.list_stashes(repo)) == 1


def test_pop_removes_stash(repo):
    make_changes(repo)
    stash.save(repo, "x")
    [s] = stash.list_stashes(repo)
    stash.pop(repo, s.index, s.sha)
    assert (root(repo) / "new.txt").exists()
    assert stash.list_stashes(repo) == []


def test_drop(repo):
    make_changes(repo)
    stash.save(repo, "throw away")
    [s] = stash.list_stashes(repo)
    assert "Deleted stash@{0}" in stash.drop(repo, s.index, s.sha)
    assert stash.list_stashes(repo) == [] and get_status(repo).clean


def test_actions_refuse_when_list_changed(repo):
    """Positions shift: an old page must not drop the wrong stash."""
    (root(repo) / "README.md").write_text("a")
    stash.save(repo, "older")
    [old] = stash.list_stashes(repo)  # older is stash@{0} now
    (root(repo) / "README.md").write_text("b")
    stash.save(repo, "newer")  # older moved to stash@{1}
    for fn in (stash.apply, stash.pop, stash.drop):
        with pytest.raises(GitError, match="changed"):
            fn(repo, old.index, old.sha)
    assert [s.title for s in stash.list_stashes(repo)] == ["newer", "older"]


@pytest.mark.parametrize("index", ["x", "", "-1", "99", None])
def test_bad_index(repo, index):
    make_changes(repo)
    stash.save(repo)
    [s] = stash.list_stashes(repo)
    with pytest.raises(GitError):
        stash.drop(repo, index, s.sha)


def test_pop_conflict_keeps_stash(repo):
    (root(repo) / "README.md").write_text("stashed version\n")
    stash.save(repo, "mine")
    (root(repo) / "README.md").write_text("committed meanwhile\n")
    run(root(repo), "commit", "-q", "-am", "other change")
    [s] = stash.list_stashes(repo)
    with pytest.raises(GitError) as e:
        stash.pop(repo, s.index, s.sha)
    assert "stash was kept" in e.value.hint
    assert len(stash.list_stashes(repo)) == 1
    assert [f.path for f in get_status(repo).conflicted] == ["README.md"]


def test_apply_blocked_by_local_changes(repo):
    (root(repo) / "README.md").write_text("stashed\n")
    stash.save(repo)
    (root(repo) / "README.md").write_text("new local edit\n")
    [s] = stash.list_stashes(repo)
    with pytest.raises(GitError):
        stash.apply(repo, s.index, s.sha)
    assert (root(repo) / "README.md").read_text() == "new local edit\n"


# --- contents ----------------------------------------------------------------------

def test_detail_lists_tracked_and_untracked(repo):
    make_changes(repo)
    stash.save(repo, "x")
    [s] = stash.list_stashes(repo)
    d = stash.detail(repo, s.index, s.sha)
    assert {(f.path, f.status) for f in d.files} == {("README.md", "modified"), ("new.txt", "untracked")}
    assert d.untracked == {"new.txt"}


def test_file_diffs(repo):
    make_changes(repo)
    stash.save(repo, "x")
    [s] = stash.list_stashes(repo)
    tracked = stash.file_diff(repo, s.index, s.sha, "README.md")
    assert [l.text for l in tracked.lines if l.kind == "add"] == ["changed"]
    assert [l.text for l in tracked.lines if l.kind == "del"] == ["hello"]
    untracked = stash.file_diff(repo, s.index, s.sha, "new.txt")
    assert [l.text for l in untracked.lines if l.kind == "add"] == ["untracked"]


def test_image_versions(repo):
    (root(repo) / "pic.png").write_bytes(PNG)
    run(root(repo), "add", "pic.png")
    run(root(repo), "commit", "-q", "-m", "pic")
    (root(repo) / "pic.png").write_bytes(PNG + b"v2")
    (root(repo) / "new.png").write_bytes(PNG)
    stash.save(repo)
    [s] = stash.list_stashes(repo)
    before, after = stash.versions(repo, s.sha, "pic.png")
    assert before and after == s.sha
    before, after = stash.versions(repo, s.sha, "new.png")
    assert before is None and after not in (None, s.sha)  # lives in the untracked parent


def test_diff_rejects_bad_path(repo):
    make_changes(repo)
    stash.save(repo)
    [s] = stash.list_stashes(repo)
    with pytest.raises(GitError):
        stash.file_diff(repo, s.index, s.sha, "../etc/passwd")
