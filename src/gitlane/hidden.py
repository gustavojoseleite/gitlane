"""Branches hidden from the graph.

The hidden set holds full ref names ("refs/heads/x", "refs/remotes/origin/x")
and may hold ALL_REMOTES. gitlane stores it per repo (see workspace.py); Git
itself is never changed. The current branch is always shown.
"""

from __future__ import annotations

import git

from .branches import list_branches
from .git_ops import GitError

ALL_REMOTES = "refs/remotes/*"
KINDS = ("branch", "remote")


def ref_name(kind: str, name: str) -> str:
    return f"refs/heads/{name}" if kind == "branch" else f"refs/remotes/{name}"


def is_hidden(hidden: set[str], kind: str, name: str) -> bool:
    if kind == "remote" and ALL_REMOTES in hidden:
        return True
    return ref_name(kind, name) in hidden


def effective(repo: git.Repo, hidden: set[str]) -> set[str]:
    """What the graph leaves out: the stored set minus the current branch."""
    if not hidden or repo.head.is_detached:
        return set(hidden)
    return hidden - {f"refs/heads/{repo.active_branch.name}"}


def _prune(repo: git.Repo, hidden: set[str]) -> set[str]:
    """Forget branches that no longer exist."""
    local, remote = list_branches(repo)
    existing = {ref_name("branch", b.name) for b in local} | {ref_name("remote", r) for r in remote}
    return {r for r in hidden if r in existing or r == ALL_REMOTES}


def _check(repo: git.Repo, kind: str, name: str):
    if kind not in KINDS:
        raise GitError("Unknown kind of branch.")
    local, remote = list_branches(repo)
    if kind == "branch":
        branch = next((b for b in local if b.name == name), None)
        if branch is None:
            raise GitError(f"Branch '{name}' not found.")
        return branch
    if name not in remote:
        raise GitError(f"Remote branch '{name}' not found.")
    return None


def hide(repo: git.Repo, hidden: set[str], kind: str, name: str) -> tuple[set[str], str]:
    """Hide a branch; a local branch takes its remote copy (upstream) with it."""
    branch = _check(repo, kind, name)
    if branch is not None and branch.current:
        raise GitError(f"{name} is the current branch, so it is always shown.",
                       hint="Switch to another branch first if you want to hide this one.")
    refs = {ref_name(kind, name)}
    if branch is not None and branch.upstream:
        refs.add(ref_name("remote", branch.upstream))
    also = f" (and {branch.upstream})" if branch is not None and branch.upstream else ""
    return _prune(repo, hidden | refs), f"Hid {name}{also} in the graph. Show it again from the sidebar."


def show(repo: git.Repo, hidden: set[str], kind: str, name: str) -> tuple[set[str], str]:
    branch = _check(repo, kind, name)
    refs = {ref_name(kind, name)}
    if branch is not None and branch.upstream:
        refs.add(ref_name("remote", branch.upstream))
    return _prune(repo, hidden - refs), f"{name} is shown in the graph again."


def show_all(repo: git.Repo, hidden: set[str]) -> tuple[set[str], str]:
    return set(), "All branches are shown in the graph again."


def set_remotes_hidden(repo: git.Repo, hidden: set[str], on: bool) -> tuple[set[str], str]:
    if on:
        return _prune(repo, hidden | {ALL_REMOTES}), "Remote branches are hidden in the graph."
    rest = {r for r in hidden if not r.startswith("refs/remotes/")}
    return _prune(repo, rest), "Remote branches are shown in the graph again."
