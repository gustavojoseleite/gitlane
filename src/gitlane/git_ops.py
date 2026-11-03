"""All Git operations used by gitlane."""

from __future__ import annotations

import os
import re
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
    # Treat every path we pass as a literal file name, never as a pattern:
    # a file called "*" must not mean "all files".
    # Never let Git wait for a password on a terminal nobody is watching.
    repo.git.update_environment(GIT_LITERAL_PATHSPECS="1", GIT_TERMINAL_PROMPT="0")
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


# --- Changes (milestone 2) ------------------------------------------------

class GitError(Exception):
    """A Git operation failed; the message is safe to show to the user.

    ``hint`` is an optional plain-language explanation shown above Git's
    own message.
    """

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


MAX_DIFF_LINES = 3000


def check_paths(paths: list[str]) -> list[str]:
    """Reject anything that is not a plain relative path inside the repo."""
    if not paths:
        raise GitError("No file selected.")
    clean = []
    for p in paths:
        if not p or "\0" in p:
            raise GitError("Invalid file name.")
        norm = os.path.normpath(p)
        if os.path.isabs(p) or norm == ".." or norm.startswith(".." + os.sep):
            raise GitError(f"Path is outside the repository: {p}")
        clean.append(p)
    return clean


def _has_head(repo: git.Repo) -> bool:
    try:
        repo.head.commit
        return True
    except ValueError:
        return False


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except git.GitCommandError as e:
        msg = "\n".join(
            part.strip().removeprefix("stderr:").removeprefix("stdout:").strip().strip("'").strip()
            for part in (e.stderr, e.stdout) if part and str(part).strip()
        ) or str(e)
        raise GitError(msg, hint=explain(msg)) from None


# Common Git failures -> plain-language hints. First match wins.
_HINTS = [
    ("Permission denied (publickey)",
     "The remote did not accept your SSH key. Check it works with "
     "`ssh -T git@github.com` (or your host) in a terminal."),
    ("Host key verification failed",
     "This SSH host is not known yet. Connect once from a terminal "
     "(for example `ssh -T git@github.com`) to accept its key, then try again."),
    ("terminal prompts disabled",
     "This HTTPS remote needs a username/password and gitlane cannot ask for it. "
     "Set up a Git credential helper or switch the remote to SSH."),
    ("could not read Username",
     "This HTTPS remote needs a username/password and gitlane cannot ask for it. "
     "Set up a Git credential helper or switch the remote to SSH."),
    ("Could not resolve host",
     "Cannot reach the remote host. Check your internet connection."),
    ("Couldn't connect to server",
     "Cannot connect to the remote server. Check the remote URL and your network."),
    ("Connection refused",
     "The remote refused the connection. Check the remote URL and your network."),
    ("Connection timed out",
     "The connection to the remote timed out. Check your network."),
    ("did not complete in",
     "The operation took too long and was stopped. Check your network and try again."),
    ("stale info",
     "The remote branch changed since your last fetch, so force push was refused "
     "to protect that work. Fetch, look at the new commits, then decide."),
    ("fetch first",
     "The remote has commits you don't have yet. Pull first, then push."),
    ("non-fast-forward",
     "The remote has commits you don't have yet. Pull first, then push."),
    ("Not possible to fast-forward",
     "Your branch and the remote have both changed, and your Git settings only allow "
     "fast-forward pulls."),
    ("divergent branches",
     "Your branch and the remote have both changed. Your Git settings decide how to "
     "combine them: set `git config pull.rebase false` to merge."),
    ("would be overwritten by merge",
     "You have uncommitted changes to files the pull wants to update. "
     "Commit or stash them first."),
    ("untracked working tree files would be overwritten",
     "Untracked files would be overwritten by the pull. Move, commit or delete them first."),
    ("CONFLICT",
     "The pull stopped with merge conflicts. Fix the conflicted files in your editor, "
     "then stage them and commit (conflict tools arrive in a later milestone)."),
    ("Automatic merge failed",
     "The pull stopped with merge conflicts. Fix the conflicted files in your editor, "
     "then stage them and commit (conflict tools arrive in a later milestone)."),
    ("does not appear to be a git repository",
     "The remote URL is wrong or you don't have access to it."),
    ("Repository not found",
     "The remote repository was not found, or you don't have access to it."),
]


def explain(message: str) -> str | None:
    for needle, hint in _HINTS:
        if needle in message:
            return hint
    return None


def stage(repo: git.Repo, paths: list[str]) -> None:
    """Stage whole files (also stages deletions)."""
    _run(repo.git.add, "--all", "--", *check_paths(paths))


def stage_all(repo: git.Repo) -> None:
    _run(repo.git.add, "--all")


def unstage(repo: git.Repo, paths: list[str]) -> None:
    paths = check_paths(paths)
    if _has_head(repo):
        _run(repo.git.restore, "--staged", "--", *paths)
    else:  # no commits yet: there is nothing to restore from
        _run(repo.git.rm, "--cached", "-q", "-r", "--", *paths)


def unstage_all(repo: git.Repo) -> None:
    if _has_head(repo):
        _run(repo.git.reset, "-q")
    else:
        _run(repo.git.rm, "--cached", "-q", "-r", "--ignore-unmatch", ".")


def discard(repo: git.Repo, path: str) -> None:
    """Throw away unstaged changes to one file. Untracked files are deleted.

    Staged changes are kept: the file goes back to its staged version.
    """
    (path,) = check_paths([path])
    if path in repo.untracked_files:
        _run(repo.git.clean, "-f", "-q", "--", path)
    else:
        _run(repo.git.restore, "--worktree", "--", path)


def commit(repo: git.Repo, message: str) -> str:
    """Commit the staged changes; returns the new commit's short hash."""
    message = message.strip()
    if not message:
        raise GitError("Write a summary for the commit message first.")
    code, _, _ = repo.git.diff("--cached", "--quiet", with_extended_output=True, with_exceptions=False)
    if code == 0:
        raise GitError("Nothing staged to commit. Stage some files first.")
    _run(repo.git.commit, "-q", "--cleanup=strip", "-m", message)
    return repo.head.commit.hexsha[:7]


@dataclass
class DiffLine:
    kind: str  # "add", "del", "ctx", "hunk", "meta"
    text: str
    old: int | None = None
    new: int | None = None


@dataclass
class FileDiff:
    path: str
    staged: bool
    lines: list[DiffLine] = field(default_factory=list)
    binary: bool = False
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not self.binary and not any(l.kind in ("add", "del") for l in self.lines)


def diff_file(repo: git.Repo, path: str, staged: bool = False) -> FileDiff:
    """Unified diff of one file: staged (index vs HEAD) or unstaged (worktree vs index).

    Untracked files are shown as entirely added.
    """
    (path,) = check_paths([path])
    opts = ["--no-color", "--no-ext-diff", "--no-textconv"]
    if not staged and path in repo.untracked_files:
        raw = repo.git.diff(*opts, "--no-index", "--", os.devnull, path,
                            with_exceptions=False, stdout_as_string=False)
    elif staged:
        raw = _run(repo.git.diff, *opts, "--cached", "--", path, stdout_as_string=False)
    else:
        raw = _run(repo.git.diff, *opts, "--", path, stdout_as_string=False)
    return parse_diff(raw.decode("utf-8", errors="replace"), path, staged)


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_diff(text: str, path: str, staged: bool) -> FileDiff:
    result = FileDiff(path=path, staged=staged)
    old = new = 0
    in_hunk = False
    for line in text.splitlines():
        if len(result.lines) >= MAX_DIFF_LINES:
            result.truncated = True
            break
        m = _HUNK.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            in_hunk = True
            result.lines.append(DiffLine("hunk", line))
        elif not in_hunk:
            if line.startswith("Binary files") or line == "GIT binary patch":
                result.binary = True
            # File headers (diff --git, index, ---/+++) are not shown.
        elif line.startswith("+"):
            result.lines.append(DiffLine("add", line[1:], new=new))
            new += 1
        elif line.startswith("-"):
            result.lines.append(DiffLine("del", line[1:], old=old))
            old += 1
        elif line.startswith(" ") or line == "":
            result.lines.append(DiffLine("ctx", line[1:], old=old, new=new))
            old += 1
            new += 1
        elif line.startswith("\\"):
            result.lines.append(DiffLine("meta", line[2:]))
        else:  # next file header (should not happen for a single path)
            in_hunk = False
            if line.startswith("Binary files"):
                result.binary = True
    return result


# --- Sync (milestone 3) ---------------------------------------------------

NETWORK_TIMEOUT = 120  # seconds


def _network_kwargs(repo: git.Repo) -> dict:
    """Extra options for commands that talk to a remote.

    SSH is put in batch mode so it fails instead of waiting for a passphrase
    or a host-key question, unless the user configured SSH themselves.
    """
    env = {}
    user_ssh = os.environ.get("GIT_SSH_COMMAND") or os.environ.get("GIT_SSH")
    if not user_ssh:
        user_ssh = _config(repo, "core.sshCommand")
    if not user_ssh:
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return {"env": env, "kill_after_timeout": NETWORK_TIMEOUT}


def _config(repo: git.Repo, key: str) -> str | None:
    """Read a Git setting the way Git does (all levels, includes); None if unset."""
    code, out, _ = repo.git.config("--get", key, with_extended_output=True, with_exceptions=False)
    return out.strip() if code == 0 else None


def _current_branch(repo: git.Repo) -> str:
    if repo.head.is_detached:
        raise GitError("You are not on a branch (detached HEAD). Switch to a branch first.")
    return repo.active_branch.name


def _upstream(repo: git.Repo, branch: str) -> str | None:
    code, out, _ = repo.git.rev_parse(
        "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}",
        with_extended_output=True, with_exceptions=False,
    )
    return out.strip() if code == 0 and out.strip() else None


def _head_sha(repo: git.Repo) -> str | None:
    return repo.head.commit.hexsha if _has_head(repo) else None


def _require_remote(repo: git.Repo) -> None:
    if not repo.remotes:
        raise GitError("This repository has no remote. Add one with "
                       "`git remote add origin <url>`.")


def fetch(repo: git.Repo) -> str:
    _require_remote(repo)
    _run(repo.git.fetch, "--all", "--prune", **_network_kwargs(repo))
    return "Fetched from all remotes."


def pull(repo: git.Repo) -> str:
    """Pull the current branch from its upstream.

    If the user has set ``pull.rebase`` or ``pull.ff`` we respect it;
    otherwise we fast-forward when possible and merge when the branches
    have diverged (like GitKraken's default).
    """
    _require_remote(repo)
    branch = _current_branch(repo)
    upstream = _upstream(repo, branch)
    if upstream is None:
        raise GitError(f"Branch '{branch}' has no upstream yet, so there is nothing to pull. "
                       "Push it first to create it on the remote.")
    configured = _config(repo, "pull.rebase") is not None or _config(repo, "pull.ff") is not None
    args = [] if configured else ["--no-rebase"]
    before = _head_sha(repo)
    _run(repo.git.pull, *args, **_network_kwargs(repo))
    after = _head_sha(repo)
    if before == after:
        return f"Already up to date with {upstream}."
    count = int(repo.git.rev_list("--count", f"{before}..{after}")) if before else 0
    return f"Pulled {count} new commit{'s' if count != 1 else ''} from {upstream}."


def push(repo: git.Repo, force: bool = False) -> str:
    """Push the current branch. Creates the upstream on first push.

    ``force`` always means ``--force-with-lease`` (never plain ``--force``).
    """
    _require_remote(repo)
    branch = _current_branch(repo)
    if not _has_head(repo):
        raise GitError("There are no commits to push yet.")
    args = ["--force-with-lease"] if force else []
    upstream = _upstream(repo, branch)
    if upstream is None:
        names = [r.name for r in repo.remotes]
        remote = "origin" if "origin" in names else (names[0] if len(names) == 1 else None)
        if remote is None:
            raise GitError(f"Branch '{branch}' has no upstream and this repo has several "
                           "remotes but none called 'origin'. Push once from a terminal "
                           f"with `git push -u <remote> {branch}`.")
        args += ["-u", remote, branch]
    _run(repo.git.push, "--porcelain", *args, **_network_kwargs(repo))
    target = _upstream(repo, branch) or branch
    return f"{'Force pushed' if force else 'Pushed'} {branch} to {target}."
