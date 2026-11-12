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


# --- untracked files option ------------------------------------------------------------

def test_save_can_keep_untracked_files(repo):
    make_changes(repo)
    msg = stash.save(repo, "", include_untracked=False)
    assert "Untracked files were kept" in msg
    status = get_status(repo)
    assert not status.unstaged and [c.path for c in status.untracked] == ["new.txt"]
    assert (root(repo) / "new.txt").read_text() == "untracked"
    [s] = stash.list_stashes(repo)
    assert [f.path for f in stash.detail(repo, 0, s.sha).files] == ["README.md"]


def test_only_untracked_files_and_box_unticked(repo):
    (root(repo) / "new.txt").write_text("untracked")
    with pytest.raises(GitError, match="Include untracked files"):
        stash.save(repo, "", include_untracked=False)
    assert stash.list_stashes(repo) == []


# --- stash only some files --------------------------------------------------------

def test_save_chosen_files_keeps_the_rest(repo):
    (root(repo) / "README.md").write_text("changed\n")
    (root(repo) / "staged.txt").write_text("staged")
    run(root(repo), "add", "staged.txt")
    (root(repo) / "new.txt").write_text("untracked")
    msg = stash.save(repo, "only readme", paths=["README.md"])
    assert msg.startswith("Stashed README.md as “only readme”.")
    assert "other changes are still here" in msg
    s = get_status(repo)
    assert [c.path for c in s.staged] == ["staged.txt"]  # still staged
    assert [c.path for c in s.untracked] == ["new.txt"]
    assert (root(repo) / "README.md").read_text() == "hello\n"
    stash.pop(repo, 0, stash.list_stashes(repo)[0].sha)
    assert (root(repo) / "README.md").read_text() == "changed\n"


def test_save_chosen_untracked_file(repo):
    make_changes(repo)
    (root(repo) / "other.txt").write_text("stays")
    stash.save(repo, paths=["new.txt"])
    assert not (root(repo) / "new.txt").exists()
    assert (root(repo) / "other.txt").exists()
    assert (root(repo) / "README.md").read_text() == "changed\n"
    with pytest.raises(GitError, match="untracked"):
        stash.save(repo, paths=["other.txt"], include_untracked=False)


def test_save_chosen_files_are_literal_paths(repo):
    """A name like "[a].txt" must not also match "a.txt" as a pattern would."""
    for name in ("a.txt", "[a].txt"):
        (root(repo) / name).write_text("v1")
    run(root(repo), "add", ".")
    run(root(repo), "commit", "-q", "-m", "two files")
    for name in ("a.txt", "[a].txt"):
        (root(repo) / name).write_text("v2")
    stash.save(repo, paths=["[a].txt"])
    assert (root(repo) / "[a].txt").read_text() == "v1"
    assert (root(repo) / "a.txt").read_text() == "v2"


def test_save_chosen_staged_rename_or_deletion_is_refused(repo):
    """Git can't stash those by name; refuse up front instead of half-stashing."""
    (root(repo) / "b.txt").write_text("b")
    run(root(repo), "add", "b.txt")
    run(root(repo), "commit", "-q", "-m", "b")
    run(root(repo), "mv", "README.md", "READ.md")
    run(root(repo), "rm", "-q", "b.txt")
    (root(repo) / "new.txt").write_text("n")
    with pytest.raises(GitError, match="renamed file") as e:
        stash.save(repo, paths=["READ.md"])
    assert "Unstage it first" in e.value.hint
    with pytest.raises(GitError, match="staged deletion"):
        stash.save(repo, paths=["b.txt", "new.txt"])
    assert stash.list_stashes(repo) == []
    assert (root(repo) / "new.txt").exists()


def test_save_chosen_unstaged_deletion(repo):
    (root(repo) / "README.md").unlink()
    (root(repo) / "new.txt").write_text("n")
    stash.save(repo, paths=["README.md"])
    assert (root(repo) / "README.md").read_text() == "hello\n"
    assert (root(repo) / "new.txt").exists()


def test_save_chosen_files_errors(repo):
    make_changes(repo)
    with pytest.raises(GitError, match="at least one"):
        stash.save(repo, paths=[])
    with pytest.raises(GitError, match="no changes"):
        stash.save(repo, paths=["nope.txt"])
    with pytest.raises(GitError):
        stash.save(repo, paths=["../outside"])
    assert stash.list_stashes(repo) == []


# --- pop onto another branch --------------------------------------------------------

def test_pop_onto_another_branch(repo):
    run(root(repo), "branch", "feature")
    make_changes(repo)
    stash.save(repo, "for feature")
    [s] = stash.list_stashes(repo)
    msg = stash.pop_onto(repo, 0, s.sha, "feature")
    assert msg == "Switched to feature, applied stash@{0} (for feature) and removed it."
    assert repo.active_branch.name == "feature"
    assert (root(repo) / "README.md").read_text() == "changed\n"
    assert stash.list_stashes(repo) == []


def test_pop_onto_current_branch(repo):
    make_changes(repo)
    stash.save(repo)
    [s] = stash.list_stashes(repo)
    assert "already on main" in stash.pop_onto(repo, 0, s.sha, "main")
    assert stash.list_stashes(repo) == []


def test_pop_onto_checks_branch_and_stash(repo):
    make_changes(repo)
    stash.save(repo)
    [s] = stash.list_stashes(repo)
    with pytest.raises(GitError, match="no local branch"):
        stash.pop_onto(repo, 0, s.sha, "nope")
    with pytest.raises(GitError, match="changed"):
        stash.pop_onto(repo, 0, "0" * 40, "main")
    assert len(stash.list_stashes(repo)) == 1


def test_pop_onto_stops_before_switching_over_your_changes(repo):
    run(root(repo), "checkout", "-q", "-b", "feature")
    (root(repo) / "README.md").write_text("feature\n")
    run(root(repo), "commit", "-q", "-am", "feature readme")
    run(root(repo), "checkout", "-q", "main")
    (root(repo) / "x.txt").write_text("stash me")
    stash.save(repo)
    (root(repo) / "README.md").write_text("mine, not committed\n")
    [s] = stash.list_stashes(repo)
    with pytest.raises(GitError):
        stash.pop_onto(repo, 0, s.sha, "feature")
    assert repo.active_branch.name == "main"
    assert (root(repo) / "README.md").read_text() == "mine, not committed\n"
    assert len(stash.list_stashes(repo)) == 1


def test_pop_into_new_branch(repo):
    (root(repo) / "README.md").write_text("from stash\n")
    stash.save(repo, "idea")
    # main moves on with a conflicting change: a plain pop would conflict
    (root(repo) / "README.md").write_text("main moved\n")
    run(root(repo), "commit", "-q", "-am", "main moved")
    base = run(root(repo), "rev-parse", "HEAD~1")
    [s] = stash.list_stashes(repo)
    msg = stash.pop_to_new_branch(repo, 0, s.sha, "idea-branch")
    assert "Created idea-branch" in msg
    assert repo.active_branch.name == "idea-branch"
    assert run(root(repo), "rev-parse", "HEAD") == base
    assert (root(repo) / "README.md").read_text() == "from stash\n"
    assert stash.list_stashes(repo) == []


def test_pop_into_new_branch_needs_clean_tree_and_good_name(repo):
    make_changes(repo)
    stash.save(repo)
    [s] = stash.list_stashes(repo)
    with pytest.raises(GitError, match="not a valid branch name"):
        stash.pop_to_new_branch(repo, 0, s.sha, "bad name")
    (root(repo) / "README.md").write_text("dirty\n")
    with pytest.raises(GitError, match="clean working tree"):
        stash.pop_to_new_branch(repo, 0, s.sha, "ok")
    assert len(stash.list_stashes(repo)) == 1
