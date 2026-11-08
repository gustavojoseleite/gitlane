"""Stash operations (milestone 6).

A stash is identified by its position (``stash@{N}``) *and* its commit id:
positions shift whenever a stash is added or dropped, so every action
checks that the stash at that position is still the one the user saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import git

from .git_ops import FileChange, FileDiff, GitError, RepoStatus, _run, check_paths, get_status, parse_diff
from .graph import EMPTY_TREE, _parse_name_status

_F = "\x1f"

# `git stash` uses the ":/" pathspec internally to clean untracked files; our
# global literal-pathspec setting would turn that into a file named ":/".
# Stash commands never receive user paths, so it is safe to disable here.
_STASH_ENV = {"env": {"GIT_LITERAL_PATHSPECS": "0"}}


@dataclass
class Stash:
    index: int
    sha: str
    message: str  # e.g. "On main: my message" or "WIP on main: abc1234 subject"
    timestamp: int

    @property
    def ref(self) -> str:
        return f"stash@{{{self.index}}}"

    @property
    def date(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M")

    @property
    def title(self) -> str:
        """The user's message without Git's "On branch:" prefix."""
        for prefix in ("On ", "WIP on "):
            if self.message.startswith(prefix) and ": " in self.message:
                return self.message.split(": ", 1)[1]
        return self.message

    @property
    def branch(self) -> str | None:
        for prefix in ("On ", "WIP on "):
            if self.message.startswith(prefix) and ": " in self.message:
                return self.message[len(prefix):].split(": ", 1)[0]
        return None


def list_stashes(repo: git.Repo) -> list[Stash]:
    code, out, _ = repo.git.stash("list", f"--format=%H{_F}%gs{_F}%ct",
                                  with_extended_output=True, with_exceptions=False)
    if code != 0:
        return []
    stashes = []
    for i, line in enumerate(out.splitlines()):
        sha, message, ts = line.split(_F, 2)
        stashes.append(Stash(index=i, sha=sha, message=message, timestamp=int(ts)))
    return stashes


def _find(repo: git.Repo, index, sha: str) -> Stash:
    try:
        index = int(index)
    except (TypeError, ValueError):
        raise GitError("Invalid stash.") from None
    stashes = list_stashes(repo)
    if not 0 <= index < len(stashes) or stashes[index].sha != sha:
        raise GitError("The stash list changed since the page was loaded. Refresh and try again.")
    return stashes[index]


def _top_stash(repo: git.Repo) -> str | None:
    code, out, _ = repo.git.rev_parse("-q", "--verify", "refs/stash",
                                      with_extended_output=True, with_exceptions=False)
    return out.strip() if code == 0 else None


def _push(repo: git.Repo, message: str, include_untracked: bool) -> RepoStatus:
    """Run ``git stash push``; returns the status from before.

    Git never stashes what happens inside a submodule (its files or its new
    commits), and it exits with success when it found nothing to save -- so
    check first, and check afterwards that a stash really was made.
    """
    status = get_status(repo)
    if status.head_commit is None:
        raise GitError("Make a first commit before stashing.")
    if status.conflicted:
        raise GitError("Resolve the conflicted files before stashing.")
    files = [c for c in status.unstaged if not c.submodule]
    if not (status.staged or files or (include_untracked and status.untracked)):
        subs = [c.path for c in status.unstaged if c.submodule]
        if subs:
            raise GitError(f"The only changes are inside {_names(subs)}, which Git doesn't stash "
                           "from here. Use “Stash changes inside” on the submodule instead.")
        if status.untracked:
            raise GitError("There are no changes to stash. Untracked files are kept unless you tick "
                           "“Include untracked files”.")
        raise GitError("There are no changes to stash.")
    args = ["push", "-q"]
    if include_untracked:
        args.append("--include-untracked")
    if message:
        args += ["-m", message]
    before = _top_stash(repo)
    _run(repo.git.stash, *args, **_STASH_ENV)
    if _top_stash(repo) == before:
        raise GitError("Git found nothing to stash.")
    return status


def _names(paths: list[str]) -> str:
    return paths[0] if len(paths) == 1 else ", ".join(paths[:-1]) + f" and {paths[-1]}"


def save(repo: git.Repo, message: str = "", include_untracked: bool = True) -> str:
    """Stash all uncommitted changes (and, by default, untracked files)."""
    message = (message or "").strip()
    _push(repo, message, include_untracked)
    text = f"Stashed your changes{f' as “{message}”' if message else ''}."
    after = get_status(repo)
    left = [c.path for c in after.unstaged if c.submodule]
    if left:
        text += (f" Changes inside {_names(left)} were not stashed: Git stashes each submodule "
                 "separately (use “Stash changes inside” on it).")
    elif after.untracked and not include_untracked:
        text += " Untracked files were kept."
    elif after.clean:
        text += " Your working tree is clean."
    return text


def save_in_submodule(repo: git.Repo, path: str, message: str = "", include_untracked: bool = True) -> str:
    """Stash the changes inside a submodule (its own stash list holds them)."""
    from .git_ops import open_repo
    from .submodules import _require

    sub = _require(repo, path)
    if not sub.ready:
        raise GitError(f"{sub.path} is not set up, so there is nothing to stash in it.")
    message = (message or "").strip()
    inner = open_repo(Path(repo.working_tree_dir) / sub.path)
    try:
        if Path(inner.working_tree_dir).resolve() != (Path(repo.working_tree_dir) / sub.path).resolve():
            raise GitError(f"{sub.path} is not checked out.")
        _push(inner, message, include_untracked)
    finally:
        inner.close()
    return (f"Stashed the changes inside {sub.path}{f' as “{message}”' if message else ''}. "
            f"Open {sub.path} in its own tab to see or apply the stash.")


def apply(repo: git.Repo, index, sha: str) -> str:
    s = _find(repo, index, sha)
    _run(repo.git.stash, "apply", "-q", s.ref, **_STASH_ENV)
    return f"Applied {s.ref} ({s.title}). The stash is kept."


def pop(repo: git.Repo, index, sha: str) -> str:
    s = _find(repo, index, sha)
    try:
        _run(repo.git.stash, "pop", "-q", s.ref, **_STASH_ENV)
    except GitError as e:
        # Newer Git versions word (or, with -q, hide) the CONFLICT line differently,
        # so look at the result instead: conflicted files mean the stash was kept.
        if "CONFLICT" in str(e) or get_status(repo).conflicted:
            e.hint = ("Applying the stash caused conflicts. Fix the conflicted files in your editor "
                      "and stage them. The stash was kept, so nothing is lost; drop it when you're done.")
        raise
    return f"Applied and removed {s.ref} ({s.title})."


def drop(repo: git.Repo, index, sha: str) -> str:
    s = _find(repo, index, sha)
    _run(repo.git.stash, "drop", "-q", s.ref, **_STASH_ENV)
    return f"Deleted {s.ref} ({s.title})."


# --- contents --------------------------------------------------------------------

@dataclass
class StashDetail:
    stash: Stash
    files: list[FileChange]
    untracked: set[str]


def _untracked_parent(repo: git.Repo, sha: str) -> str | None:
    parents = repo.commit(sha).parents
    return parents[2].hexsha if len(parents) > 2 else None


def detail(repo: git.Repo, index, sha: str) -> StashDetail:
    s = _find(repo, index, sha)
    base = repo.commit(s.sha).parents[0].hexsha
    files = _parse_name_status(repo.git.diff("--name-status", "-z", "-M", "--no-ext-diff", base, s.sha))
    untracked: set[str] = set()
    u = _untracked_parent(repo, s.sha)
    if u:
        for f in _parse_name_status(repo.git.diff_tree("--root", "-r", "--name-status", "-z",
                                                       "--no-commit-id", u)):
            files.append(FileChange(f.path, "untracked"))
            untracked.add(f.path)
    return StashDetail(stash=s, files=files, untracked=untracked)


def versions(repo: git.Repo, sha: str, path: str) -> tuple[str | None, str | None]:
    """(before, after) commit ids for a file in a stash; None where it doesn't exist."""
    (path,) = check_paths([path])

    def has(rev: str) -> bool:
        code, _, _ = repo.git.cat_file("-e", f"{rev}:{path}", with_extended_output=True,
                                       with_exceptions=False)
        return code == 0

    u = _untracked_parent(repo, sha)
    if u and has(u):
        return None, u
    base = repo.commit(sha).parents[0].hexsha
    return (base if has(base) else None), (sha if has(sha) else None)


def file_diff(repo: git.Repo, index, sha: str, path: str) -> FileDiff:
    s = _find(repo, index, sha)
    (path,) = check_paths([path])
    opts = ["--no-color", "--no-ext-diff", "--no-textconv", "-M"]
    u = _untracked_parent(repo, s.sha)
    before, after = versions(repo, s.sha, path)
    if u and after == u:
        raw = _run(repo.git.diff, *opts, EMPTY_TREE, u, "--", path, stdout_as_string=False)
    else:
        base = repo.commit(s.sha).parents[0].hexsha
        raw = _run(repo.git.diff, *opts, base, s.sha, "--", path, stdout_as_string=False)
    return parse_diff(raw.decode("utf-8", errors="replace"), path, staged=False)
