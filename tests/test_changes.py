from __future__ import annotations

import os

import pytest

from gitlane import git_ops
from gitlane.git_ops import GitError, get_status, open_repo

from .conftest import run


@pytest.fixture
def repo(make_repo):
    r = open_repo(make_repo())
    yield r
    r.close()


@pytest.fixture
def empty_repo(make_repo):
    r = open_repo(make_repo(commit=False))
    yield r
    r.close()


def paths(files):
    return sorted(f.path for f in files)


def root(r):
    from pathlib import Path
    return Path(r.working_tree_dir)


# --- stage / unstage ---------------------------------------------------------

def test_stage_and_unstage_file(repo):
    (root(repo) / "README.md").write_text("changed\n")
    git_ops.stage(repo, ["README.md"])
    s = get_status(repo)
    assert paths(s.staged) == ["README.md"] and not s.unstaged
    git_ops.unstage(repo, ["README.md"])
    s = get_status(repo)
    assert paths(s.unstaged) == ["README.md"] and not s.staged


def test_stage_deleted_file(repo):
    (root(repo) / "README.md").unlink()
    git_ops.stage(repo, ["README.md"])
    s = get_status(repo)
    assert [(f.path, f.status) for f in s.staged] == [("README.md", "deleted")]


def test_stage_all_and_unstage_all(repo):
    (root(repo) / "README.md").write_text("changed\n")
    (root(repo) / "new.txt").write_text("n")
    git_ops.stage_all(repo)
    s = get_status(repo)
    assert paths(s.staged) == ["README.md", "new.txt"]
    assert not s.unstaged and not s.untracked
    git_ops.unstage_all(repo)
    s = get_status(repo)
    assert not s.staged
    assert paths(s.unstaged) == ["README.md"] and paths(s.untracked) == ["new.txt"]


def test_unstage_in_repo_without_commits(empty_repo):
    (root(empty_repo) / "a.txt").write_text("a")
    (root(empty_repo) / "b.txt").write_text("b")
    git_ops.stage_all(empty_repo)
    git_ops.unstage(empty_repo, ["a.txt"])
    s = get_status(empty_repo)
    assert paths(s.staged) == ["b.txt"] and paths(s.untracked) == ["a.txt"]
    git_ops.unstage_all(empty_repo)
    assert paths(get_status(empty_repo).untracked) == ["a.txt", "b.txt"]


def test_unstage_rename_restores_both_paths(repo):
    run(root(repo), "mv", "README.md", "DOCS.md")
    git_ops.unstage(repo, ["DOCS.md", "README.md"])
    s = get_status(repo)
    assert not s.staged
    assert [(f.path, f.status) for f in s.unstaged] == [("README.md", "deleted")]
    assert paths(s.untracked) == ["DOCS.md"]


@pytest.mark.skipif(os.name == "nt", reason="Windows doesn't allow '*' in file names")
def test_pathspec_magic_is_literal(repo):
    """A file named '*' must not stage every file."""
    (root(repo) / "*").write_text("star")
    (root(repo) / "other.txt").write_text("o")
    git_ops.stage(repo, ["*"])
    s = get_status(repo)
    assert paths(s.staged) == ["*"]
    assert paths(s.untracked) == ["other.txt"]
    with pytest.raises(GitError):
        git_ops.stage(repo, [":(glob)*.txt"])
    assert paths(get_status(repo).untracked) == ["other.txt"]


@pytest.mark.parametrize("bad", ["../outside.txt", "/etc/passwd", "a/../../x", "", "a\0b"])
def test_paths_outside_repo_rejected(repo, bad):
    with pytest.raises(GitError):
        git_ops.stage(repo, [bad])


def test_no_paths_rejected(repo):
    with pytest.raises(GitError, match="No file"):
        git_ops.stage(repo, [])


def test_option_like_file_name(repo):
    (root(repo) / "--force").write_text("x")
    git_ops.stage(repo, ["--force"])
    assert paths(get_status(repo).staged) == ["--force"]


# --- commit ------------------------------------------------------------------

def test_commit(repo):
    (root(repo) / "README.md").write_text("changed\n")
    git_ops.stage(repo, ["README.md"])
    sha = git_ops.commit(repo, "  Update readme\n\nLonger body.\n  ")
    s = get_status(repo)
    assert s.clean and s.head_commit.startswith(sha)
    assert repo.head.commit.message == "Update readme\n\nLonger body.\n"


def test_first_commit_in_empty_repo(empty_repo):
    (root(empty_repo) / "a.txt").write_text("a")
    git_ops.stage_all(empty_repo)
    git_ops.commit(empty_repo, "First")
    assert get_status(empty_repo).head_summary == "First"


def test_commit_message_starting_with_dash(repo):
    (root(repo) / "x").write_text("x")
    git_ops.stage_all(repo)
    git_ops.commit(repo, "-- not an option")
    assert repo.head.commit.summary == "-- not an option"


def test_commit_requires_message(repo):
    (root(repo) / "x").write_text("x")
    git_ops.stage_all(repo)
    with pytest.raises(GitError, match="message"):
        git_ops.commit(repo, "   \n ")


@pytest.mark.parametrize("make_empty", [False, True])
def test_commit_requires_staged_changes(make_repo, make_empty):
    r = open_repo(make_repo(commit=not make_empty))
    (root(r) / "unstaged.txt").write_text("u")
    with pytest.raises(GitError, match="Nothing staged"):
        git_ops.commit(r, "msg")
    r.close()


def test_commit_hook_failure_is_reported(repo):
    hook = root(repo) / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint failed: fix your code' >&2\nexit 1\n")
    hook.chmod(0o755)
    (root(repo) / "x").write_text("x")
    git_ops.stage_all(repo)
    with pytest.raises(GitError, match="lint failed"):
        git_ops.commit(repo, "msg")


# --- discard -----------------------------------------------------------------

def test_discard_modified_file(repo):
    (root(repo) / "README.md").write_text("changed\n")
    git_ops.discard(repo, "README.md")
    assert (root(repo) / "README.md").read_text() == "hello\n"
    assert get_status(repo).clean


def test_discard_keeps_staged_version(repo):
    f = root(repo) / "README.md"
    f.write_text("staged\n")
    git_ops.stage(repo, ["README.md"])
    f.write_text("staged then edited\n")
    git_ops.discard(repo, "README.md")
    assert f.read_text() == "staged\n"
    s = get_status(repo)
    assert paths(s.staged) == ["README.md"] and not s.unstaged


def test_discard_deleted_file_restores_it(repo):
    (root(repo) / "README.md").unlink()
    git_ops.discard(repo, "README.md")
    assert (root(repo) / "README.md").exists()


def test_discard_untracked_deletes_only_that_file(repo):
    (root(repo) / "junk.txt").write_text("j")
    (root(repo) / "keep.txt").write_text("k")
    git_ops.discard(repo, "junk.txt")
    assert not (root(repo) / "junk.txt").exists()
    assert (root(repo) / "keep.txt").exists()


def test_discard_rejects_outside_path(repo):
    with pytest.raises(GitError):
        git_ops.discard(repo, "../../etc/passwd")


# --- diff --------------------------------------------------------------------

def test_unstaged_diff(repo):
    (root(repo) / "README.md").write_text("hello\nworld\n")
    d = git_ops.diff_file(repo, "README.md")
    assert not d.staged and not d.empty
    assert [(l.kind, l.text, l.new) for l in d.lines if l.kind == "add"] == [("add", "world", 2)]
    assert d.lines[0].kind == "hunk"


def test_staged_diff_and_line_numbers(repo):
    f = root(repo) / "README.md"
    f.write_text("hello\n")
    (root(repo) / "nums.txt").write_text("".join(f"{i}\n" for i in range(1, 11)))
    git_ops.stage_all(repo)
    git_ops.commit(repo, "nums")
    (root(repo) / "nums.txt").write_text("".join(f"{i}\n" for i in range(1, 11) if i != 5))
    git_ops.stage(repo, ["nums.txt"])
    assert git_ops.diff_file(repo, "nums.txt", staged=False).empty
    d = git_ops.diff_file(repo, "nums.txt", staged=True)
    assert [(l.text, l.old) for l in d.lines if l.kind == "del"] == [("5", 5)]


def test_untracked_diff_shows_whole_file(repo):
    (root(repo) / "new.txt").write_text("a\nb\n")
    d = git_ops.diff_file(repo, "new.txt")
    assert [l.text for l in d.lines if l.kind == "add"] == ["a", "b"]


def test_binary_diff(repo):
    (root(repo) / "img.bin").write_bytes(bytes(range(256)))
    assert git_ops.diff_file(repo, "img.bin").binary


def test_invalid_utf8_does_not_crash(repo):
    (root(repo) / "latin1.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    d = git_ops.diff_file(repo, "latin1.txt")
    assert d.lines[-1].text == "caf�"


def test_no_newline_marker(repo):
    (root(repo) / "README.md").write_text("hello")
    d = git_ops.diff_file(repo, "README.md")
    assert any(l.kind == "meta" and "No newline" in l.text for l in d.lines)


def test_large_diff_truncated(repo):
    (root(repo) / "big.txt").write_text("x\n" * (git_ops.MAX_DIFF_LINES + 500))
    d = git_ops.diff_file(repo, "big.txt")
    assert d.truncated and len(d.lines) == git_ops.MAX_DIFF_LINES


def test_diff_of_unchanged_file_is_empty(repo):
    assert git_ops.diff_file(repo, "README.md").empty
