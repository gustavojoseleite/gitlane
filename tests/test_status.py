from __future__ import annotations

import pytest

from gitlane.git_ops import NotAGitRepo, get_status, open_repo

from .conftest import run


def status_of(path):
    repo = open_repo(path)
    try:
        return get_status(repo)
    finally:
        repo.close()


def test_clean_repo(make_repo):
    path = make_repo()
    s = status_of(path)
    assert s.branch == "main"
    assert s.name == path.name
    assert s.head_summary == "Initial commit"
    assert len(s.head_commit) == 40
    assert s.clean and not s.detached
    assert s.upstream is None


def test_empty_repo_has_no_commit(make_repo):
    s = status_of(make_repo(commit=False))
    assert s.branch == "main"
    assert s.head_commit is None and s.head_summary is None
    assert not s.detached


def test_file_categories(make_repo):
    path = make_repo()
    (path / "README.md").write_text("changed\n")  # unstaged modification
    (path / "new.txt").write_text("x")
    run(path, "add", "new.txt")  # staged addition
    (path / "dir with space").mkdir()
    (path / "dir with space" / "file ünï.txt").write_text("u")  # untracked, odd name
    s = status_of(path)
    assert [(f.path, f.status) for f in s.unstaged] == [("README.md", "modified")]
    assert [(f.path, f.status) for f in s.staged] == [("new.txt", "added")]
    assert [f.path for f in s.untracked] == ["dir with space/file ünï.txt"]
    assert not s.clean


def test_staged_and_unstaged_same_file(make_repo):
    path = make_repo()
    (path / "README.md").write_text("one\n")
    run(path, "add", "README.md")
    (path / "README.md").write_text("two\n")
    s = status_of(path)
    assert [f.path for f in s.staged] == ["README.md"]
    assert [f.path for f in s.unstaged] == ["README.md"]


def test_rename_and_delete(make_repo):
    path = make_repo()
    (path / "b.txt").write_text("bbb\n" * 20)
    run(path, "add", "b.txt")
    run(path, "commit", "-q", "-m", "add b")
    run(path, "mv", "b.txt", "c.txt")
    run(path, "rm", "-q", "README.md")
    s = status_of(path)
    staged = {f.path: f for f in s.staged}
    assert staged["c.txt"].status == "renamed"
    assert staged["c.txt"].orig_path == "b.txt"
    assert staged["README.md"].status == "deleted"


def test_detached_head(make_repo):
    path = make_repo()
    sha = run(path, "rev-parse", "HEAD")
    run(path, "checkout", "-q", "--detach")
    s = status_of(path)
    assert s.branch is None and s.detached
    assert s.head_commit == sha


def test_ahead_behind_with_bare_remote(make_repo, tmp_path):
    path = make_repo()
    remote = tmp_path / "remote.git"
    run(tmp_path, "init", "-q", "--bare", str(remote))
    run(path, "remote", "add", "origin", str(remote))
    run(path, "push", "-q", "-u", "origin", "main")

    # Another clone pushes one commit -> we are behind after fetch.
    other = tmp_path / "other"
    run(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "o.txt").write_text("o")
    run(other, "add", "o.txt")
    run(other, "commit", "-q", "-m", "other")
    run(other, "push", "-q")

    # We commit twice locally -> ahead 2.
    for n in ("1", "2"):
        (path / f"{n}.txt").write_text(n)
        run(path, "add", ".")
        run(path, "commit", "-q", "-m", n)
    run(path, "fetch", "-q")

    s = status_of(path)
    assert s.upstream == "origin/main"
    assert (s.ahead, s.behind) == (2, 1)


def test_merge_conflict_listed(make_repo):
    path = make_repo()
    run(path, "checkout", "-q", "-b", "feature")
    (path / "README.md").write_text("feature\n")
    run(path, "commit", "-q", "-am", "feature")
    run(path, "checkout", "-q", "main")
    (path / "README.md").write_text("main\n")
    run(path, "commit", "-q", "-am", "main")
    with pytest.raises(Exception):
        run(path, "merge", "-q", "feature")
    s = status_of(path)
    assert [f.path for f in s.conflicted] == ["README.md"]


def test_open_from_subfolder(make_repo):
    path = make_repo()
    sub = path / "a" / "b"
    sub.mkdir(parents=True)
    assert status_of(sub).root == str(path.resolve())


def test_not_a_repo(tmp_path):
    with pytest.raises(NotAGitRepo, match="Not a Git repository"):
        open_repo(tmp_path)


def test_missing_folder(tmp_path):
    with pytest.raises(NotAGitRepo, match="does not exist"):
        open_repo(tmp_path / "nope")


def test_bare_repo_rejected(tmp_path):
    run(tmp_path, "init", "-q", "--bare", "b.git")
    with pytest.raises(NotAGitRepo, match="Bare"):
        open_repo(tmp_path / "b.git")
