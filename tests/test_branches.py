from __future__ import annotations

from pathlib import Path

import pytest

from gitlane import branches
from gitlane.git_ops import GitError, get_status, open_repo

from .conftest import run


@pytest.fixture
def repo(make_repo):
    r = open_repo(make_repo())
    yield r
    r.close()


def root(r) -> Path:
    return Path(r.working_tree_dir)


def commit(r, name, content=None, msg=None):
    (root(r) / name).write_text(content if content is not None else name)
    run(root(r), "add", name)
    run(root(r), "commit", "-q", "-m", msg or f"edit {name}")
    return run(root(r), "rev-parse", "HEAD")


def head_branch(r):
    return run(root(r), "branch", "--show-current")


def conflict_setup(r):
    """main and feature both change README.md differently."""
    run(root(r), "checkout", "-q", "-b", "feature")
    commit(r, "README.md", "feature\n")
    run(root(r), "checkout", "-q", "main")
    commit(r, "README.md", "main\n")


# --- listing & names -----------------------------------------------------------

def test_list_branches(repo, tmp_path):
    run(root(repo), "branch", "feature")
    run(tmp_path, "init", "-q", "--bare", "r.git")
    run(root(repo), "remote", "add", "origin", str(tmp_path / "r.git"))
    run(root(repo), "push", "-q", "-u", "origin", "main")
    run(root(repo), "remote", "set-head", "origin", "main")
    commit(repo, "a.txt")
    local, remote = branches.list_branches(repo)
    assert [(b.name, b.current) for b in local] == [("feature", False), ("main", True)]
    main = local[1]
    assert main.upstream == "origin/main" and main.ahead == 1 and main.behind == 0
    assert remote == ["origin/main"]  # origin/HEAD hidden


@pytest.mark.parametrize("bad", ["", "  ", "has space", "a..b", "-x", "x~1", "a:b", "end.lock", "x^"])
def test_invalid_branch_names(repo, bad):
    with pytest.raises(GitError):
        branches.create_branch(repo, bad)


# --- create / checkout -------------------------------------------------------------

def test_create_and_switch(repo):
    msg = branches.create_branch(repo, "feature/login")
    assert "feature/login" in msg and head_branch(repo) == "feature/login"


def test_create_at_commit(repo):
    first = run(root(repo), "rev-parse", "HEAD")
    commit(repo, "a.txt")
    branches.create_branch(repo, "old", start=first)
    assert run(root(repo), "rev-parse", "HEAD") == first


def test_create_duplicate(repo):
    branches.create_branch(repo, "x", checkout=False)
    with pytest.raises(GitError) as e:
        branches.create_branch(repo, "x")
    assert "already exists" in e.value.hint


def test_create_in_empty_repo(make_repo):
    r = open_repo(make_repo(commit=False))
    with pytest.raises(GitError, match="first commit"):
        branches.create_branch(r, "x")
    r.close()


def test_checkout_local(repo):
    run(root(repo), "branch", "other")
    assert branches.checkout(repo, "other") == "Switched to other."
    assert head_branch(repo) == "other"


def test_checkout_unknown_branch(repo):
    for name in ("nope", "--orphan", "HEAD~1"):
        with pytest.raises(GitError, match="not found"):
            branches.checkout(repo, name)


def test_checkout_remote_creates_tracking_branch(repo, tmp_path):
    run(tmp_path, "init", "-q", "--bare", "r.git")
    run(root(repo), "remote", "add", "origin", str(tmp_path / "r.git"))
    run(root(repo), "push", "-q", "origin", "main:feature")
    run(root(repo), "fetch", "-q")
    msg = branches.checkout(repo, "origin/feature")
    assert "tracking origin/feature" in msg
    assert head_branch(repo) == "feature"
    assert get_status(repo).upstream == "origin/feature"
    run(root(repo), "checkout", "-q", "main")
    assert "existing local branch" in branches.checkout(repo, "origin/feature")


def test_checkout_blocked_by_local_changes(repo):
    run(root(repo), "checkout", "-q", "-b", "other")
    commit(repo, "README.md", "other\n")
    run(root(repo), "checkout", "-q", "main")
    (root(repo) / "README.md").write_text("uncommitted\n")
    with pytest.raises(GitError) as e:
        branches.checkout(repo, "other")
    assert "Commit or stash" in e.value.hint
    assert (root(repo) / "README.md").read_text() == "uncommitted\n"


def test_checkout_commit_detached(repo):
    first = run(root(repo), "rev-parse", "HEAD")
    commit(repo, "a.txt")
    assert "detached" in branches.checkout_commit(repo, first[:7])
    assert get_status(repo).detached


# --- merge ---------------------------------------------------------------------------

def test_merge_fast_forward(repo):
    run(root(repo), "checkout", "-q", "-b", "feature")
    commit(repo, "f.txt")
    run(root(repo), "checkout", "-q", "main")
    assert branches.merge(repo, "feature") == "Merged feature into main."
    assert (root(repo) / "f.txt").exists()


def test_merge_creates_merge_commit_without_editor(repo):
    run(root(repo), "checkout", "-q", "-b", "feature")
    commit(repo, "f.txt")
    run(root(repo), "checkout", "-q", "main")
    commit(repo, "m.txt")
    branches.merge(repo, "feature")  # would hang if Git opened an editor
    assert len(repo.head.commit.parents) == 2


def test_merge_already_up_to_date(repo):
    run(root(repo), "branch", "same")
    assert "already contains" in branches.merge(repo, "same")


def test_merge_into_itself_and_detached(repo):
    with pytest.raises(GitError, match="itself"):
        branches.merge(repo, "main")
    run(root(repo), "branch", "x")
    run(root(repo), "checkout", "-q", "--detach")
    with pytest.raises(GitError, match="detached"):
        branches.merge(repo, "x")


def test_merge_conflict_then_abort(repo):
    conflict_setup(repo)
    with pytest.raises(GitError) as e:
        branches.merge(repo, "feature")
    assert "Mark resolved" in e.value.hint
    s = get_status(repo)
    assert s.operation == "merge" and [f.path for f in s.conflicted] == ["README.md"]
    assert "aborted" in branches.abort_operation(repo)
    s = get_status(repo)
    assert s.operation is None and s.clean
    assert (root(repo) / "README.md").read_text() == "main\n"


def test_merge_conflict_resolve_and_continue(repo):
    conflict_setup(repo)
    with pytest.raises(GitError):
        branches.merge(repo, "feature")
    with pytest.raises(GitError) as e:  # markers still there
        branches.continue_operation(repo)
    assert "conflict markers" in str(e.value) and "README.md" in str(e.value)
    (root(repo) / "README.md").write_text("main and feature\n")
    assert branches.continue_operation(repo) == "Merge completed."
    s = get_status(repo)
    assert s.operation is None and s.clean
    assert len(repo.head.commit.parents) == 2


def test_no_merge_while_operation_in_progress(repo):
    conflict_setup(repo)
    run(root(repo), "branch", "other")
    with pytest.raises(GitError):
        branches.merge(repo, "feature")
    with pytest.raises(GitError, match="in progress"):
        branches.merge(repo, "other")


def test_abort_or_continue_with_nothing(repo):
    with pytest.raises(GitError, match="nothing to abort"):
        branches.abort_operation(repo)
    with pytest.raises(GitError, match="nothing to continue"):
        branches.continue_operation(repo)


# --- delete --------------------------------------------------------------------------

def test_delete_merged_branch(repo):
    run(root(repo), "branch", "done")
    assert branches.delete_branch(repo, "done") == "Deleted branch done."
    assert "done" not in [b.name for b in branches.list_branches(repo)[0]]


def test_delete_unmerged_offers_force(repo):
    run(root(repo), "checkout", "-q", "-b", "wip")
    commit(repo, "w.txt")
    run(root(repo), "checkout", "-q", "main")
    with pytest.raises(GitError) as e:
        branches.delete_branch(repo, "wip")
    assert "not merged" in e.value.hint
    assert e.value.action["vals"] == {"name": "wip", "force": "1"}
    assert e.value.action["confirm"]
    branches.delete_branch(repo, "wip", force=True)
    assert "wip" not in [b.name for b in branches.list_branches(repo)[0]]


def test_cannot_delete_current_or_unknown(repo):
    with pytest.raises(GitError, match="current branch"):
        branches.delete_branch(repo, "main")
    with pytest.raises(GitError, match="not found"):
        branches.delete_branch(repo, "nope")


# --- cherry-pick / revert / reset -------------------------------------------------------

def test_cherry_pick(repo):
    run(root(repo), "checkout", "-q", "-b", "feature")
    sha = commit(repo, "fix.txt", msg="Important fix")
    run(root(repo), "checkout", "-q", "main")
    assert "Cherry-picked" in branches.cherry_pick(repo, sha)
    assert repo.head.commit.summary == "Important fix" and (root(repo) / "fix.txt").exists()


def test_cherry_pick_conflict_continue(repo):
    run(root(repo), "checkout", "-q", "-b", "feature")
    sha = commit(repo, "README.md", "feature\n", msg="feature change")
    run(root(repo), "checkout", "-q", "main")
    commit(repo, "README.md", "main\n")
    with pytest.raises(GitError):
        branches.cherry_pick(repo, sha)
    assert get_status(repo).operation == "cherry-pick"
    (root(repo) / "README.md").write_text("resolved\n")
    assert branches.continue_operation(repo) == "Cherry-pick completed."
    assert repo.head.commit.summary == "feature change"
    assert get_status(repo).operation is None


def test_revert(repo):
    sha = commit(repo, "oops.txt")
    assert "Reverted" in branches.revert(repo, sha)
    assert not (root(repo) / "oops.txt").exists()
    assert repo.head.commit.summary.startswith("Revert")


def test_merge_commits_refused_for_cherry_pick_and_revert(repo):
    run(root(repo), "checkout", "-q", "-b", "f")
    commit(repo, "f.txt")
    run(root(repo), "checkout", "-q", "main")
    commit(repo, "m.txt")
    run(root(repo), "merge", "-q", "--no-edit", "f")
    sha = run(root(repo), "rev-parse", "HEAD")
    for fn in (branches.cherry_pick, branches.revert):
        with pytest.raises(GitError, match="merge commits"):
            fn(repo, sha)


@pytest.mark.parametrize("mode, staged, unstaged, file_exists", [
    ("soft", ["a.txt"], [], True),
    ("mixed", [], [], True),
    ("hard", [], [], False),
])
def test_reset_modes(repo, mode, staged, unstaged, file_exists):
    first = run(root(repo), "rev-parse", "HEAD")
    commit(repo, "a.txt")
    msg = branches.reset(repo, first, mode)
    assert msg == f"Reset main to {first[:7]} ({mode})."
    s = get_status(repo)
    assert s.head_commit == first
    assert [f.path for f in s.staged] == staged
    assert (root(repo) / "a.txt").exists() == file_exists
    if mode == "mixed":
        assert [f.path for f in s.untracked] == ["a.txt"]


def test_reset_bad_mode(repo):
    with pytest.raises(GitError, match="mode"):
        branches.reset(repo, run(root(repo), "rev-parse", "HEAD"), "--hard")


def test_editor_never_opened(repo):
    env = repo.git._environment
    assert env["GIT_EDITOR"] == "true" and env["GIT_MERGE_AUTOEDIT"] == "no"
