from __future__ import annotations

from pathlib import Path

import pytest

from gitlane import tags
from gitlane.git_ops import GitError, open_repo

from .conftest import run


@pytest.fixture
def repo(make_repo):
    r = open_repo(make_repo())
    yield r
    r.close()


def root(r) -> Path:
    return Path(r.working_tree_dir)


def commit(r, name):
    (root(r) / name).write_text(name)
    run(root(r), "add", name)
    run(root(r), "commit", "-q", "-m", f"add {name}")
    return run(root(r), "rev-parse", "HEAD")


@pytest.fixture
def with_remote(repo, tmp_path):
    run(tmp_path, "init", "-q", "--bare", "server.git")
    run(root(repo), "remote", "add", "origin", str(tmp_path / "server.git"))
    run(root(repo), "push", "-q", "-u", "origin", "main")
    return tmp_path / "server.git"


def remote_tags(server: Path) -> list[str]:
    return run(server, "tag", "--list").split()


# --- create & list -------------------------------------------------------------

def test_lightweight_tag(repo):
    first = run(root(repo), "rev-parse", "HEAD")
    commit(repo, "b.txt")
    assert tags.create_tag(repo, "v0.1", first) == f"Created tag v0.1 at {first[:7]}."
    assert run(root(repo), "cat-file", "-t", "v0.1") == "commit"  # no tag object
    [t] = tags.list_tags(repo)
    assert (t.name, t.sha, t.annotated, t.message) == ("v0.1", first, False, "")


def test_annotated_tag(repo):
    sha = run(root(repo), "rev-parse", "HEAD")
    tags.create_tag(repo, "release/1.0", sha, "  First release\n\nWith notes.  ")
    assert run(root(repo), "cat-file", "-t", "release/1.0") == "tag"
    assert run(root(repo), "tag", "-l", "--format=%(contents)", "release/1.0").startswith("First release")
    [t] = tags.list_tags(repo)
    assert (t.sha, t.annotated, t.message) == (sha, True, "First release")


def test_list_skips_tags_on_trees(repo):
    sha = run(root(repo), "rev-parse", "HEAD")
    run(root(repo), "tag", "tree-tag", run(root(repo), "rev-parse", "HEAD^{tree}"))
    tags.create_tag(repo, "a", sha, "on a commit")
    assert [t.name for t in tags.list_tags(repo)] == ["a"]


def test_duplicate_tag(repo):
    sha = run(root(repo), "rev-parse", "HEAD")
    tags.create_tag(repo, "v1", sha)
    with pytest.raises(GitError, match="already exists") as e:
        tags.create_tag(repo, "v1", sha, "again")
    assert "delete the old tag" in e.value.hint


@pytest.mark.parametrize("bad", ["", "   ", "has space", "a..b", "-v1", "v1~", "x:y", "HEAD", "end.lock"])
def test_invalid_tag_names(repo, bad):
    sha = run(root(repo), "rev-parse", "HEAD")
    with pytest.raises(GitError):
        tags.create_tag(repo, bad, sha)
    assert tags.list_tags(repo) == []


def test_bad_commit_and_empty_repo(repo, make_repo):
    with pytest.raises(GitError):
        tags.create_tag(repo, "v1", "not-a-sha")
    empty = open_repo(make_repo("empty", commit=False))
    try:
        with pytest.raises(GitError, match="first commit"):
            tags.create_tag(empty, "v1", "abcd1234")
    finally:
        empty.close()


def test_delete_tag(repo):
    sha = run(root(repo), "rev-parse", "HEAD")
    tags.create_tag(repo, "v1", sha)
    assert "Deleted tag v1" in tags.delete_tag(repo, "v1")
    assert tags.list_tags(repo) == []
    with pytest.raises(GitError, match="not found"):
        tags.delete_tag(repo, "v1")


# --- remote ------------------------------------------------------------------------

def test_push_tag(repo, with_remote):
    sha = run(root(repo), "rev-parse", "HEAD")
    tags.create_tag(repo, "v1", sha, "one")
    tags.create_tag(repo, "v2", sha)
    assert tags.push_tag(repo, "v1") == "Pushed tag v1 to origin."
    assert remote_tags(with_remote) == ["v1"]  # only the chosen tag
    assert tags.push_tag(repo, "v1") == "Pushed tag v1 to origin."  # again: nothing to do, no error


def test_push_tag_that_differs_on_remote(repo, with_remote):
    first = run(root(repo), "rev-parse", "HEAD")
    tags.create_tag(repo, "v1", first)
    tags.push_tag(repo, "v1")
    tags.delete_tag(repo, "v1")
    tags.create_tag(repo, "v1", commit(repo, "b.txt"))
    with pytest.raises(GitError) as e:
        tags.push_tag(repo, "v1")
    assert "already has a different tag" in e.value.hint
    assert run(with_remote, "rev-parse", "v1") == first


def test_push_unknown_tag_and_no_remote(repo):
    with pytest.raises(GitError, match="not found"):
        tags.push_tag(repo, "nope")
    tags.create_tag(repo, "v1", run(root(repo), "rev-parse", "HEAD"))
    with pytest.raises(GitError, match="no remote"):
        tags.push_tag(repo, "v1")


def test_remote_choice(repo, tmp_path):
    run(tmp_path, "init", "-q", "--bare", "up.git")
    run(root(repo), "remote", "add", "upstream", str(tmp_path / "up.git"))
    tags.create_tag(repo, "v1", run(root(repo), "rev-parse", "HEAD"))
    assert tags.push_tag(repo, "v1") == "Pushed tag v1 to upstream."  # the only remote
    run(tmp_path, "init", "-q", "--bare", "other.git")
    run(root(repo), "remote", "add", "other", str(tmp_path / "other.git"))
    with pytest.raises(GitError, match="none called 'origin'"):
        tags.push_tag(repo, "v1")


def test_delete_remote_tag(repo, with_remote):
    tags.create_tag(repo, "v1", run(root(repo), "rev-parse", "HEAD"))
    tags.push_tag(repo, "v1")
    assert tags.delete_remote_tag(repo, "v1") == "Deleted tag v1 on origin. Your local tag is kept."
    assert remote_tags(with_remote) == []
    assert [t.name for t in tags.list_tags(repo)] == ["v1"]
    assert "not on origin" in tags.delete_remote_tag(repo, "v1")


def test_delete_remote_tag_without_local_copy(repo, with_remote):
    tags.create_tag(repo, "v1", run(root(repo), "rev-parse", "HEAD"))
    tags.push_tag(repo, "v1")
    tags.delete_tag(repo, "v1")
    assert tags.delete_remote_tag(repo, "v1") == "Deleted tag v1 on origin."
    with pytest.raises(GitError):
        tags.delete_remote_tag(repo, "--all")
