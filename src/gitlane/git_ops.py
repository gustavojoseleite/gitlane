"""All Git operations used by gitlane.

Read-only in milestone 1: finding the repository and reading its status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import git


class NotAGitRepo(Exception):
    """Raised when a folder is not inside a usable Git working tree."""


def open_repo(path: str | Path) -> git.Repo:
    """Open the repository that contains ``path`` (searching parent folders)."""
    path = Path(path).resolve()
    if not path.exists():
        raise NotAGitRepo(f"Folder does not exist: {path}")
    try:
        repo = git.Repo(path, search_parent_directories=True)
    except (git.InvalidGitRepositoryError, git.NoSuchPathError):
        raise NotAGitRepo(f"Not a Git repository: {path}") from None
    if repo.bare:
        raise NotAGitRepo(f"Bare repositories are not supported: {path}")
    return repo


@dataclass
class FileChange:
    path: str
    status: str  # human label: modified, added, deleted, renamed, ...
    orig_path: str | None = None  # for renames/copies


@dataclass
class RepoStatus:
    root: str
    name: str
    branch: str | None  # None when HEAD is detached
    head_commit: str | None  # None when the repo has no commits yet
    head_summary: str | None
    upstream: str | None
    ahead: int = 0
    behind: int = 0
    staged: list[FileChange] = field(default_factory=list)
    unstaged: list[FileChange] = field(default_factory=list)
    untracked: list[FileChange] = field(default_factory=list)
    conflicted: list[FileChange] = field(default_factory=list)

    @property
    def detached(self) -> bool:
        return self.branch is None and self.head_commit is not None

    @property
    def clean(self) -> bool:
        return not (self.staged or self.unstaged or self.untracked or self.conflicted)


_CODES = {
    "M": "modified",
    "T": "type changed",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
}


def get_status(repo: git.Repo) -> RepoStatus:
    """Read branch info and file changes using ``git status --porcelain=v2``."""
    raw = repo.git.status(
        "--porcelain=v2", "--branch", "-z", "--untracked-files=all",
        strip_newline_in_stdout=False,
    )
    root = Path(repo.working_tree_dir)
    status = RepoStatus(
        root=str(root), name=root.name, branch=None, head_commit=None,
        head_summary=None, upstream=None,
    )

    entries = raw.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        kind = entry[0]
        if kind == "#":
            _parse_header(entry, status)
        elif kind == "1":
            # 1 XY sub mH mI mW hH hI path
            parts = entry.split(" ", 8)
            _add_change(status, parts[1], parts[8])
        elif kind == "2":
            # 2 XY sub mH mI mW hH hI Xscore path  (orig path is the next NUL entry)
            parts = entry.split(" ", 9)
            orig = entries[i] if i < len(entries) else None
            i += 1
            _add_change(status, parts[1], parts[9], orig)
        elif kind == "u":
            # u XY sub m1 m2 m3 mW h1 h2 h3 path
            parts = entry.split(" ", 10)
            status.conflicted.append(FileChange(parts[10], "conflict"))
        elif kind == "?":
            status.untracked.append(FileChange(entry[2:], "untracked"))
        # "!" (ignored) is not requested, so nothing else to handle.

    if status.head_commit is not None:
        status.head_summary = repo.head.commit.summary
    return status


def _parse_header(entry: str, status: RepoStatus) -> None:
    _, key, *rest = entry.split(" ", 2)
    value = rest[0] if rest else ""
    if key == "branch.oid":
        status.head_commit = None if value == "(initial)" else value
    elif key == "branch.head":
        status.branch = None if value == "(detached)" else value
    elif key == "branch.upstream":
        status.upstream = value
    elif key == "branch.ab":
        ahead, behind = value.split()
        status.ahead = int(ahead.lstrip("+"))
        status.behind = int(behind.lstrip("-"))


def _add_change(status: RepoStatus, xy: str, path: str, orig: str | None = None) -> None:
    x, y = xy[0], xy[1]
    if x != ".":
        status.staged.append(FileChange(path, _CODES.get(x, x), orig if x in "RC" else None))
    if y != ".":
        status.unstaged.append(FileChange(path, _CODES.get(y, y)))
