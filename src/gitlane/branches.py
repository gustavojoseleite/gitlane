"""Branch and commit operations (milestone 5).

Every function returns a short message for the UI or raises GitError.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import git

from .git_ops import (GitError, _has_head, _network_kwargs, _run, check_paths, get_status,
                      operation_in_progress)
from .graph import resolve_commit

RESET_MODES = ("soft", "mixed", "hard")


@dataclass
class Branch:
    name: str
    current: bool = False
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    gone: bool = False  # upstream was deleted on the remote


def list_branches(repo: git.Repo) -> tuple[list[Branch], list[str]]:
    """Local branches and remote-tracking branch names (without */HEAD)."""
    out = repo.git.for_each_ref(
        "--format=%(refname)%00%(HEAD)%00%(upstream:short)%00%(upstream:track,nobracket)",
        "refs/heads", "refs/remotes",
    )
    local, remote = [], []
    for line in out.splitlines():
        ref, head, upstream, track = (line.split("\0") + ["", "", ""])[:4]
        if ref.startswith("refs/heads/"):
            b = Branch(ref[len("refs/heads/"):], current=head == "*", upstream=upstream or None)
            b.gone = track == "gone"
            for m in re.finditer(r"(ahead|behind) (\d+)", track):
                setattr(b, m.group(1), int(m.group(2)))
            local.append(b)
        elif ref.startswith("refs/remotes/") and not ref.endswith("/HEAD"):
            remote.append(ref[len("refs/remotes/"):])
    return local, remote


def check_branch_name(repo: git.Repo, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise GitError("Type a name for the branch.")
    code, _, _ = repo.git.check_ref_format("--branch", name,
                                           with_extended_output=True, with_exceptions=False)
    if code != 0 or name.startswith("-"):
        raise GitError(f"'{name}' is not a valid branch name.",
                       hint="Branch names can't contain spaces, '..', '~', '^', ':', '?', '*', '[' "
                            "or start with '-'. Try something like feature/login.")
    return name


def _local_names(repo: git.Repo) -> list[str]:
    return [b.name for b in list_branches(repo)[0]]


def _require_local(repo: git.Repo, name: str) -> str:
    if name not in _local_names(repo):
        raise GitError(f"Branch '{name}' not found.")
    return name


def _require_branch_or_remote(repo: git.Repo, name: str) -> str:
    local, remote = list_branches(repo)
    if name not in [b.name for b in local] and name not in remote:
        raise GitError(f"Branch '{name}' not found.")
    return name


def _current_branch_name(repo: git.Repo) -> str | None:
    return None if repo.head.is_detached else repo.active_branch.name


def _no_operation(repo: git.Repo) -> None:
    op = operation_in_progress(repo)
    if op:
        raise GitError(f"A {op} is in progress. Finish it or abort it first.")


# --- branches ----------------------------------------------------------------

def create_branch(repo: git.Repo, name: str, start: str | None = None, checkout: bool = True) -> str:
    name = check_branch_name(repo, name)
    if not _has_head(repo):
        raise GitError("Make a first commit before creating branches.")
    args = [resolve_commit(repo, start)] if start else []
    if checkout:
        _run(repo.git.switch, "-q", "-c", name, *args)
        return f"Created branch {name} and switched to it."
    _run(repo.git.branch, name, *args)
    return f"Created branch {name}."


def checkout(repo: git.Repo, name: str) -> str:
    """Switch to a local branch, or to a remote branch (creating a local one)."""
    _require_branch_or_remote(repo, name)
    local_names = _local_names(repo)
    if name in local_names:
        _run(repo.git.switch, "-q", name)
        return f"Switched to {name}."
    # Remote branch like origin/feature -> local "feature" tracking it.
    remotes = sorted((r.name for r in repo.remotes), key=len, reverse=True)
    remote = next(r for r in remotes if name.startswith(r + "/"))
    local = name[len(remote) + 1:]
    if local in local_names:
        _run(repo.git.switch, "-q", local)
        return f"Switched to your existing local branch {local}."
    _run(repo.git.switch, "-q", "-c", local, "--track", name)
    return f"Created local branch {local} tracking {name} and switched to it."


def checkout_commit(repo: git.Repo, sha: str) -> str:
    sha = resolve_commit(repo, sha)
    _run(repo.git.switch, "-q", "--detach", sha)
    return f"Checked out {sha[:7]} (detached HEAD). Create a branch to keep new commits."


def merge(repo: git.Repo, name: str) -> str:
    _require_branch_or_remote(repo, name)
    current = _current_branch_name(repo)
    if current is None:
        raise GitError("You are not on a branch (detached HEAD). Switch to a branch first.")
    if name == current:
        raise GitError("A branch can't be merged into itself.")
    _no_operation(repo)
    before = repo.head.commit.hexsha
    _run(repo.git.merge, "--no-edit", name)
    if repo.head.commit.hexsha == before:
        return f"{current} already contains everything in {name}."
    return f"Merged {name} into {current}."


def delete_branch(repo: git.Repo, name: str, force: bool = False) -> str:
    _require_local(repo, name)
    if name == _current_branch_name(repo):
        raise GitError(f"You can't delete {name} because it is the current branch. "
                       "Switch to another branch first.")
    try:
        _run(repo.git.branch, "-D" if force else "-d", name)
    except GitError as e:
        if "not fully merged" in str(e):
            e.action = {
                "label": "Delete anyway", "url": "/actions/branch/delete",
                "vals": {"name": name, "force": "1"},
                "confirm": f"Delete {name} anyway?\n\nIts unmerged commits will no longer be on any "
                           "branch and may be lost.",
            }
        raise
    return f"Deleted branch {name}."


def _split_remote(repo: git.Repo, name: str) -> tuple[str, str]:
    """"origin/feature/x" -> ("origin", "feature/x"), using the real remote names."""
    remotes = sorted((r.name for r in repo.remotes), key=len, reverse=True)
    remote = next((r for r in remotes if name.startswith(r + "/")), None)
    if remote is None:
        raise GitError(f"Remote branch '{name}' not found.")
    return remote, name[len(remote) + 1:]


def delete_remote_branch(repo: git.Repo, name: str) -> str:
    """Delete a branch on the remote server (``git push <remote> --delete <branch>``)."""
    if name not in list_branches(repo)[1]:
        raise GitError(f"Remote branch '{name}' not found.")
    remote, branch = _split_remote(repo, name)
    code, default, _ = repo.git.symbolic_ref("--short", "-q", f"refs/remotes/{remote}/HEAD",
                                            with_extended_output=True, with_exceptions=False)
    if code == 0 and default.strip() == name:
        raise GitError(f"{branch} is the default branch of {remote}, so it can't be deleted here.",
                       hint="Pick another default branch in the repository settings on GitHub "
                            "(or your Git host) first.")
    try:
        _run(repo.git.push, remote, "--delete", branch, **_network_kwargs(repo))
    except GitError as e:
        if "remote ref does not exist" not in str(e):
            raise
        # Someone already deleted it on the server: just drop our stale copy.
        _run(repo.git.branch, "-r", "-d", name)
        return f"{branch} was already gone from {remote}; removed the old copy of {name}."
    kept = [b.name for b in list_branches(repo)[0] if b.upstream == name]
    note = f" Your local branch {kept[0]} is kept." if kept else ""
    return f"Deleted {branch} on {remote}.{note}"


# --- commit operations ---------------------------------------------------------

def _single_parent(repo: git.Repo, sha: str, what: str) -> str:
    sha = resolve_commit(repo, sha)
    if len(repo.commit(sha).parents) > 1:
        raise GitError(f"{what} merge commits isn't supported yet. Use a terminal for this one.")
    return sha


def cherry_pick(repo: git.Repo, sha: str) -> str:
    _no_operation(repo)
    sha = _single_parent(repo, sha, "Cherry-picking")
    _run(repo.git.cherry_pick, sha)
    return f"Cherry-picked {sha[:7]} onto {_current_branch_name(repo) or 'HEAD'}."


def revert(repo: git.Repo, sha: str) -> str:
    _no_operation(repo)
    sha = _single_parent(repo, sha, "Reverting")
    _run(repo.git.revert, "--no-edit", sha)
    return f"Reverted {sha[:7]} with a new commit."


def reset(repo: git.Repo, sha: str, mode: str) -> str:
    if mode not in RESET_MODES:
        raise GitError("Unknown reset mode.")
    _no_operation(repo)
    sha = resolve_commit(repo, sha)
    _run(repo.git.reset, "-q", f"--{mode}", sha)
    where = _current_branch_name(repo) or "HEAD"
    return f"Reset {where} to {sha[:7]} ({mode})."


# --- merge conflicts ------------------------------------------------------------

_MARKER = re.compile(rb"^(<{7} |>{7} |={7}$)", re.MULTILINE)


def abort_operation(repo: git.Repo) -> str:
    op = operation_in_progress(repo)
    if op is None:
        raise GitError("There is nothing to abort.")
    _run(getattr(repo.git, op.replace("-", "_")), "--abort")
    return f"{op.capitalize()} aborted. Everything is back to how it was before."


def continue_operation(repo: git.Repo) -> str:
    """Stage the conflicted files and finish the merge / cherry-pick / revert."""
    op = operation_in_progress(repo)
    if op is None:
        raise GitError("There is nothing to continue.")
    if op == "rebase":
        raise GitError("Finishing a rebase isn't supported yet. Use `git rebase --continue` "
                       "in a terminal, or abort it here.")
    conflicted = [f.path for f in get_status(repo).conflicted]
    root = Path(repo.working_tree_dir)
    still = [p for p in check_paths(conflicted) if (root / p).is_file()
             and _MARKER.search((root / p).read_bytes())] if conflicted else []
    if still:
        raise GitError(
            "These files still contain conflict markers (<<<<<<<, =======, >>>>>>>): "
            + ", ".join(still),
            hint="Open them in your editor, keep the right lines, delete the markers, save, "
                 "then try again.",
        )
    if conflicted:
        _run(repo.git.add, "--all", "--", *conflicted)
    if op == "merge":
        _run(repo.git.commit, "--no-edit", "-q")
    else:
        _run(getattr(repo.git, op.replace("-", "_")), "--continue")
    return f"{op.capitalize()} completed."
