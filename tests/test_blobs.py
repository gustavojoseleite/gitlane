from __future__ import annotations

import os

import pytest

from gitlane import blobs
from gitlane.git_ops import GitError, open_repo

from .conftest import run

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf000002020100e221bc330000000049454e44ae426082"
)


@pytest.fixture
def repo(make_repo):
    r = open_repo(make_repo())
    yield r
    r.close()


def root(r):
    from pathlib import Path
    return Path(r.working_tree_dir)


def test_image_type():
    assert blobs.image_type("a/b/Logo.PNG") == "image/png"
    assert blobs.image_type("x.svg") == "image/svg+xml"
    assert blobs.image_type("README.md") is None


def test_read_each_version(repo):
    f = root(repo) / "img.png"
    f.write_bytes(PNG)
    run(root(repo), "add", "img.png")
    run(root(repo), "commit", "-q", "-m", "add image")
    sha = run(root(repo), "rev-parse", "HEAD")
    f.write_bytes(PNG + b"staged")
    run(root(repo), "add", "img.png")
    f.write_bytes(PNG + b"worktree")
    assert blobs.read_blob(repo, sha, "img.png") == PNG
    assert blobs.read_blob(repo, blobs.HEAD, "img.png") == PNG
    assert blobs.read_blob(repo, blobs.INDEX, "img.png") == PNG + b"staged"
    assert blobs.read_blob(repo, blobs.WORKTREE, "img.png") == PNG + b"worktree"


def test_trailing_newline_preserved(repo):
    (root(repo) / "nl.svg").write_bytes(b"<svg/>\n\n")
    run(root(repo), "add", "nl.svg")
    assert blobs.read_blob(repo, blobs.INDEX, "nl.svg") == b"<svg/>\n\n"


def test_missing_file_is_none(repo):
    sha = run(root(repo), "rev-parse", "HEAD")
    assert blobs.read_blob(repo, sha, "nope.png") is None
    assert blobs.read_blob(repo, blobs.WORKTREE, "nope.png") is None
    assert not blobs.blob_exists(repo, sha, "nope.png")
    assert blobs.blob_exists(repo, sha, "README.md")


def test_symlink_outside_repo_blocked(repo, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    os.symlink(secret, root(repo) / "link.png")
    assert blobs.read_blob(repo, blobs.WORKTREE, "link.png") is None


@pytest.mark.parametrize("rev", ["--all", "HEAD~1", "main", "zzz", ""])
def test_bad_revisions_rejected(repo, rev):
    with pytest.raises(GitError):
        blobs.read_blob(repo, rev, "README.md")


def test_path_outside_rejected(repo):
    with pytest.raises(GitError):
        blobs.read_blob(repo, blobs.WORKTREE, "../x.png")


def test_too_large(repo, monkeypatch):
    monkeypatch.setattr(blobs, "MAX_BLOB_BYTES", 10)
    (root(repo) / "big.png").write_bytes(b"x" * 20)
    run(root(repo), "add", "big.png")
    with pytest.raises(GitError, match="too large"):
        blobs.read_blob(repo, blobs.WORKTREE, "big.png")
    with pytest.raises(GitError, match="too large"):
        blobs.read_blob(repo, blobs.INDEX, "big.png")
