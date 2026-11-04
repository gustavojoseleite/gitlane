from __future__ import annotations

from pathlib import Path

import pytest

from gitlane import git_ops
from gitlane.git_ops import GitError, get_status, open_repo

from .conftest import run


class Setup:
    """A local repo tracking a bare 'origin', plus a teammate's clone."""

    def __init__(self, tmp_path: Path, make_repo):
        self.path = make_repo()
        self.remote = tmp_path / "remote.git"
        run(tmp_path, "init", "-q", "--bare", str(self.remote))
        run(self.path, "remote", "add", "origin", str(self.remote))
        run(self.path, "push", "-q", "-u", "origin", "main")
        self.other = tmp_path / "other"
        run(tmp_path, "clone", "-q", str(self.remote), str(self.other))
        self.repo = open_repo(self.path)

    def commit_local(self, name: str, content: str = "x") -> None:
        (self.path / name).write_text(content)
        run(self.path, "add", name)
        run(self.path, "commit", "-q", "-m", f"local {name}")

    def teammate_pushes(self, name: str, content: str = "t") -> None:
        run(self.other, "pull", "-q", "--no-rebase")
        (self.other / name).write_text(content)
        run(self.other, "add", name)
        run(self.other, "commit", "-q", "-m", f"teammate {name}")
        run(self.other, "push", "-q")

    def remote_head(self) -> str:
        return run(self.remote, "rev-parse", "main")


@pytest.fixture
def s(tmp_path, make_repo):
    setup = Setup(tmp_path, make_repo)
    yield setup
    setup.repo.close()


# --- fetch -------------------------------------------------------------------

def test_fetch_updates_behind_count(s):
    s.teammate_pushes("t.txt")
    assert get_status(s.repo).behind == 0
    assert "Fetched" in git_ops.fetch(s.repo)
    assert get_status(s.repo).behind == 1
    assert not (s.path / "t.txt").exists()  # fetch never touches files


def test_fetch_without_remote(make_repo):
    r = open_repo(make_repo())
    with pytest.raises(GitError, match="no remote"):
        git_ops.fetch(r)
    r.close()


# --- pull --------------------------------------------------------------------

def test_pull_fast_forward(s):
    s.teammate_pushes("t1.txt")
    s.teammate_pushes("t2.txt")
    msg = git_ops.pull(s.repo)
    assert "Pulled 2 new commits" in msg
    assert (s.path / "t2.txt").exists()
    assert get_status(s.repo).behind == 0


def test_pull_up_to_date(s):
    assert "Already up to date" in git_ops.pull(s.repo)


def test_pull_diverged_merges_by_default(s):
    s.commit_local("mine.txt")
    s.teammate_pushes("theirs.txt")
    msg = git_ops.pull(s.repo)
    assert "Pulled" in msg
    assert (s.path / "theirs.txt").exists() and (s.path / "mine.txt").exists()
    assert len(s.repo.head.commit.parents) == 2  # merge commit
    st = get_status(s.repo)
    assert (st.ahead, st.behind) == (2, 0)


def test_pull_respects_ff_only_config(s):
    run(s.path, "config", "pull.ff", "only")
    s.commit_local("mine.txt")
    s.teammate_pushes("theirs.txt")
    with pytest.raises(GitError) as e:
        git_ops.pull(s.repo)
    assert "fast-forward" in e.value.hint


def test_pull_with_local_changes_in_the_way(s):
    s.teammate_pushes("README.md", "teammate version\n")
    (s.path / "README.md").write_text("my uncommitted edit\n")
    with pytest.raises(GitError) as e:
        git_ops.pull(s.repo)
    assert "Commit or stash" in e.value.hint
    assert (s.path / "README.md").read_text() == "my uncommitted edit\n"


def test_pull_conflict_reported_and_visible(s):
    s.commit_local("README.md", "mine\n")
    s.teammate_pushes("README.md", "theirs\n")
    with pytest.raises(GitError) as e:
        git_ops.pull(s.repo)
    assert "conflicts" in e.value.hint
    assert [f.path for f in get_status(s.repo).conflicted] == ["README.md"]


def test_pull_without_upstream(s):
    run(s.path, "checkout", "-q", "-b", "feature")
    with pytest.raises(GitError, match="no upstream"):
        git_ops.pull(s.repo)


def test_pull_detached(s):
    run(s.path, "checkout", "-q", "--detach")
    with pytest.raises(GitError, match="detached"):
        git_ops.pull(s.repo)


# --- push --------------------------------------------------------------------

def test_push(s):
    s.commit_local("a.txt")
    msg = git_ops.push(s.repo)
    assert msg == "Pushed main to origin/main."
    assert s.remote_head() == s.repo.head.commit.hexsha
    assert get_status(s.repo).ahead == 0


def test_first_push_creates_upstream(s):
    run(s.path, "checkout", "-q", "-b", "feature")
    s.commit_local("f.txt")
    assert git_ops.push(s.repo) == "Pushed feature to origin/feature."
    assert get_status(s.repo).upstream == "origin/feature"


def test_first_push_with_single_non_origin_remote(tmp_path, make_repo):
    path = make_repo()
    run(tmp_path, "init", "-q", "--bare", "up.git")
    run(path, "remote", "add", "upstream", str(tmp_path / "up.git"))
    r = open_repo(path)
    assert git_ops.push(r) == "Pushed main to upstream/main."
    r.close()


def test_first_push_ambiguous_remotes(tmp_path, make_repo):
    path = make_repo()
    for name in ("a", "b"):
        run(tmp_path, "init", "-q", "--bare", f"{name}.git")
        run(path, "remote", "add", name, str(tmp_path / f"{name}.git"))
    r = open_repo(path)
    with pytest.raises(GitError, match="several"):
        git_ops.push(r)
    r.close()


def test_push_rejected_when_behind(s):
    s.teammate_pushes("t.txt")
    s.commit_local("mine.txt")
    with pytest.raises(GitError) as e:
        git_ops.push(s.repo)
    assert "Pull first" in e.value.hint


def test_push_without_remote(make_repo):
    r = open_repo(make_repo())
    with pytest.raises(GitError, match="no remote"):
        git_ops.push(r)
    r.close()


def test_push_detached(s):
    run(s.path, "checkout", "-q", "--detach")
    with pytest.raises(GitError, match="detached"):
        git_ops.push(s.repo)


# --- force push --------------------------------------------------------------

def test_force_push_after_amend(s):
    s.commit_local("a.txt", "v1")
    git_ops.push(s.repo)
    (s.path / "a.txt").write_text("v2")
    run(s.path, "commit", "-q", "-a", "--amend", "-m", "amended")
    with pytest.raises(GitError):
        git_ops.push(s.repo)  # normal push is rejected
    assert git_ops.push(s.repo, force=True) == "Force pushed main to origin/main."
    assert s.remote_head() == s.repo.head.commit.hexsha


def test_force_push_lease_protects_unseen_work(s):
    s.commit_local("a.txt", "v1")
    git_ops.push(s.repo)
    run(s.path, "commit", "-q", "--amend", "-m", "amended")
    s.teammate_pushes("their-work.txt")  # we have NOT fetched this
    before = s.remote_head()
    with pytest.raises(GitError) as e:
        git_ops.push(s.repo, force=True)
    assert "changed since your last fetch" in e.value.hint
    assert s.remote_head() == before  # teammate's work is safe


# --- no hanging, readable errors ------------------------------------------------

def test_terminal_prompts_disabled(s):
    assert s.repo.git._environment["GIT_TERMINAL_PROMPT"] == "0"


def test_ssh_batch_mode_unless_user_configured(s, monkeypatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    assert git_ops._network_kwargs(s.repo)["env"] == {"GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
    run(s.path, "config", "core.sshCommand", "ssh -i ~/.ssh/work_key")
    assert git_ops._network_kwargs(s.repo)["env"] == {}
    run(s.path, "config", "--unset", "core.sshCommand")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -v")
    assert git_ops._network_kwargs(s.repo)["env"] == {}


def test_unreachable_https_remote_fails_fast_with_hint(s):
    run(s.path, "remote", "set-url", "origin", "https://127.0.0.1:1/nope.git")
    with pytest.raises(GitError) as e:
        git_ops.fetch(s.repo)
    assert e.value.hint is not None


@pytest.mark.parametrize("message, expected", [
    ("git@github.com: Permission denied (publickey).", "SSH key"),
    ("Host key verification failed.", "not known yet"),
    ("fatal: could not read Username for 'https://github.com': terminal prompts disabled", "credential helper"),
    ("ssh: Could not resolve hostname github.com", "internet"),
    ("fatal: unable to access 'https://127.0.0.1:1/x.git/': Failed to connect to 127.0.0.1 port 1 "
     "after 0 ms: Could not connect to server", "Cannot connect"),  # curl 8
    ("! refs/heads/main:refs/heads/main [rejected] (stale info)", "last fetch"),
    ("something nobody has seen before", None),
])
def test_explain(message, expected):
    hint = git_ops.explain(message)
    if expected is None:
        assert hint is None
    else:
        assert expected in hint
