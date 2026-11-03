"""Stash operations (milestone 6).

A stash is identified by its position (``stash@{N}``) *and* its commit id:
positions shift whenever a stash is added or dropped, so every action
checks that the stash at that position is still the one the user saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import git

from .git_ops import FileChange, FileDiff, GitError, _run, check_paths, get_status, parse_diff
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


def save(repo: git.Repo, message: str = "", include_untracked: bool = True) -> str:
    """Stash all uncommitted changes (and, by default, untracked files)."""
    status = get_status(repo)
    if status.head_commit is None:
        raise GitError("Make a first commit before stashing.")
    if status.conflicted:
        raise GitError("Resolve the conflicted files before stashing.")
    nothing = not (status.staged or status.unstaged or (include_untracked and status.untracked))
    if nothing:
        raise GitError("There are no changes to stash.")
    args = ["push", "-q"]
    if include_untracked:
        args.append("--include-untracked")
    message = (message or "").strip()
    if message:
        args += ["-m", message]
    _run(repo.git.stash, *args, **_STASH_ENV)
    return f"Stashed your changes{f' as “{message}”' if message else ''}. Your working tree is clean."


def apply(repo: git.Repo, index, sha: str) -> str:
    s = _find(repo, index, sha)
    _run(repo.git.stash, "apply", "-q", s.ref, **_STASH_ENV)
    return f"Applied {s.ref} ({s.title}). The stash is kept."


def pop(repo: git.Repo, index, sha: str) -> str:
    s = _find(repo, index, sha)
    try:
        _run(repo.git.stash, "pop", "-q", s.ref, **_STASH_ENV)
    except GitError as e:
        if "CONFLICT" in str(e):
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
