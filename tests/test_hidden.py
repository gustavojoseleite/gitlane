from __future__ import annotations

from pathlib import Path

import pytest

from gitlane import graph, hidden
from gitlane.git_ops import GitError, open_repo
from gitlane.workspace import Workspace

from .conftest import run


@pytest.fixture
def repo(make_repo, tmp_path):
    """main + feature (own commit) + other (own commit); origin has main and feature."""
    path = make_repo()
    for b in ("feature", "other"):
        run(path, "switch", "-q", "-c", b, "main")
        run(path, "commit", "-q", "--allow-empty", "-m", f"on {b}")
    run(path, "switch", "-q", "main")
    run(tmp_path, "init", "-q", "--bare", "server.git")
    run(path, "remote", "add", "origin", str(tmp_path / "server.git"))
    run(path, "push", "-q", "-u", "origin", "main", "feature")
    run(path, "push", "-q", "origin", "other:only-remote")
    run(path, "fetch", "-q")
    r = open_repo(path)
    yield r
    r.close()


def subjects(repo, refs=frozenset()) -> list[str]:
    return [row.commit.subject for row in graph.graph_page(repo, hidden=refs).rows]


def labels(repo, refs=frozenset()) -> set[str]:
    return {r.name for row in graph.graph_page(repo, hidden=refs).rows for r in row.commit.refs}


def test_hide_local_takes_its_upstream(repo):
    refs, msg = hidden.hide(repo, set(), "branch", "feature")
    assert refs == {"refs/heads/feature", "refs/remotes/origin/feature"}
    assert msg.startswith("Hid feature (and origin/feature)")
    assert "on feature" not in subjects(repo, refs)
    assert "on other" in subjects(repo, refs)
    refs, msg = hidden.show(repo, refs, "branch", "feature")
    assert refs == set() and "shown" in msg
    assert "on feature" in subjects(repo, refs)


def test_label_dropped_when_commit_stays_visible(repo):
    run(Path(repo.working_tree_dir), "branch", "same-as-main")
    refs, _ = hidden.hide(repo, set(), "branch", "same-as-main")
    assert "same-as-main" not in labels(repo, refs)
    assert "Initial commit" in subjects(repo, refs)  # main still reaches it


def test_hide_remote_only(repo):
    refs, _ = hidden.hide(repo, set(), "remote", "origin/only-remote")
    assert refs == {"refs/remotes/origin/only-remote"}
    assert "origin/only-remote" not in labels(repo, refs)
    assert "on other" in subjects(repo, refs)  # the local branch "other" still shows it


def test_hide_all_remotes(repo):
    refs, msg = hidden.set_remotes_hidden(repo, {"refs/remotes/origin/only-remote", "refs/heads/other"}, True)
    assert hidden.ALL_REMOTES in refs and "hidden" in msg
    assert not any(n.startswith("origin/") for n in labels(repo, refs))
    assert hidden.is_hidden(refs, "remote", "origin/main")
    refs, _ = hidden.set_remotes_hidden(repo, refs, False)
    assert refs == {"refs/heads/other"}  # local choices are kept


def test_current_branch_is_never_hidden(repo):
    with pytest.raises(GitError, match="current branch") as e:
        hidden.hide(repo, set(), "branch", "main")
    assert "Switch to another branch" in e.value.hint
    # Hidden earlier, then checked out: shown while it is the current branch.
    refs, _ = hidden.hide(repo, set(), "branch", "feature")
    run(Path(repo.working_tree_dir), "switch", "-q", "feature")
    assert "refs/heads/feature" not in hidden.effective(repo, refs)
    assert "on feature" in subjects(repo, hidden.effective(repo, refs))


def test_unknown_names_and_kinds(repo):
    for kind, name in [("branch", "nope"), ("remote", "origin/nope"), ("tag", "main"), ("branch", "origin/main")]:
        with pytest.raises(GitError):
            hidden.hide(repo, set(), kind, name)


def test_deleted_branches_are_forgotten(repo):
    refs, _ = hidden.hide(repo, set(), "branch", "other")
    run(Path(repo.working_tree_dir), "branch", "-D", "other")
    refs, _ = hidden.hide(repo, refs, "branch", "feature")
    assert "refs/heads/other" not in refs


def test_show_all(repo):
    refs, _ = hidden.hide(repo, set(), "branch", "other")
    assert hidden.show_all(repo, refs)[0] == set()


def test_paging_still_works_with_hidden(repo):
    refs, _ = hidden.hide(repo, set(), "branch", "feature")
    first = graph.graph_page(repo, 0, 1, hidden=refs)
    second = graph.graph_page(repo, 1, 1, hidden=refs)
    assert first.has_more and len(second.rows) == 1
    assert first.rows[0].commit.sha != second.rows[0].commit.sha


def test_workspace_remembers_hidden(tmp_path, make_repo):
    path = make_repo("r2")
    state = tmp_path / "state.json"
    ws = Workspace(state)
    ws.set_hidden(path, {"refs/heads/a", "refs/remotes/*"})
    assert Workspace(state).hidden_of(path) == {"refs/heads/a", "refs/remotes/*"}
    ws.set_hidden(path, set())
    assert Workspace(state).hidden_of(path) == set()
    state.write_text('{"hidden": {"x": "not a list", "y": [1, "refs/heads/b"]}}')
    assert Workspace(state).hidden == {"y": ["refs/heads/b"]}
