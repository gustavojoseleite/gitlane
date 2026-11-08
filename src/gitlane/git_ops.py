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
    # ...and never open an editor (merge/revert/cherry-pick would wait forever).
    # Reads (status) run while an action may be working, so they must not take
    # index.lock just to refresh the index: that could make a pull or commit fail.
    repo.git.update_environment(
        GIT_LITERAL_PATHSPECS="1", GIT_TERMINAL_PROMPT="0",
        GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true", GIT_MERGE_AUTOEDIT="no",
        GIT_OPTIONAL_LOCKS="0",
    )
    return repo


@dataclass
class FileChange:
    path: str
    status: str  # human label: modified, added, deleted, renamed, ...
    orig_path: str | None = None  # for renames/copies
    submodule: str | None = None  # for a submodule: what changed, e.g. "new commits, untracked files"


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
    operation: str | None = None  # "merge", "cherry-pick", "revert", "rebase"

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
            _add_change(status, parts[1], parts[8], sub=parts[2])
        elif kind == "2":
            # 2 XY sub mH mI mW hH hI Xscore path  (orig path is the next NUL entry)
            parts = entry.split(" ", 9)
            orig = entries[i] if i < len(entries) else None
            i += 1
            _add_change(status, parts[1], parts[9], orig, sub=parts[2])
        elif kind == "u":
            # u XY sub m1 m2 m3 mW h1 h2 h3 path
            parts = entry.split(" ", 10)
            status.conflicted.append(FileChange(parts[10], "conflict"))
        elif kind == "?":
            status.untracked.append(FileChange(entry[2:], "untracked"))
        # "!" (ignored) is not requested, so nothing else to handle.

    if status.head_commit is not None:
        status.head_summary = repo.head.commit.summary
    status.operation = operation_in_progress(repo)
    return status


def operation_in_progress(repo: git.Repo) -> str | None:
    git_dir = Path(repo.git_dir)
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return "rebase"
    for name, op in (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                     ("REVERT_HEAD", "revert")):
        if (git_dir / name).exists():
            return op
    return None


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


def _submodule_note(sub: str) -> str:
    """Porcelain v2 "sub" field (S<C><M><U>) -> plain words."""
    words = [w for flag, w in zip(sub[1:], ("new commits", "changed files inside", "untracked files inside"))
             if flag != "."]
    return ", ".join(words)


def _add_change(status: RepoStatus, xy: str, path: str, orig: str | None = None, sub: str = "N...") -> None:
    x, y = xy[0], xy[1]
    is_sub = sub.startswith("S")
    if x != ".":
        status.staged.append(FileChange(path, _CODES.get(x, x), orig if x in "RC" else None,
                                        submodule="commit changed" if is_sub else None))
    if y != ".":
        status.unstaged.append(FileChange(path, _CODES.get(y, y),
                                          submodule=(_submodule_note(sub) or "changed") if is_sub else None))


# --- Changes (milestone 2) ------------------------------------------------

class GitError(Exception):
    """A Git operation failed; the message is safe to show to the user.

    ``hint`` is an optional plain-language explanation shown above Git's
    own message.
    """

    def __init__(self, message: str, hint: str | None = None, action: dict | None = None):
        super().__init__(message)
        self.hint = hint
        # Optional follow-up button: {"label", "url", "vals", "confirm"}
        self.action = action


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
    ("Could not connect to server",  # curl 8 wording
     "Cannot connect to the remote server. Check the remote URL and your network."),
    ("Failed to connect to",
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
    ("would be overwritten by checkout",
     "You have uncommitted changes that conflict with that branch. Commit or stash them first."),
    ("not fully merged",
     "This branch has commits that are not merged into the current branch. "
     "Deleting it may lose them."),
    ("Cannot delete branch",
     "You can't delete the branch you are on. Switch to another branch first."),
    ("already exists",
     "A branch with that name already exists."),
    ("previous cherry-pick is now empty",
     "The changes from this commit are already in your branch, so there is nothing to apply."),
    ("CONFLICT",
     "Git stopped because of conflicts. Fix the conflicted files in your editor, then click "
     "\"Mark resolved and commit\" — or click \"Abort\" to go back to how things were."),
    ("Automatic merge failed",
     "Git stopped because of conflicts. Fix the conflicted files in your editor, then click "
     "\"Mark resolved and commit\" — or click \"Abort\" to go back to how things were."),
    ("after resolving the conflicts",
     "Git stopped because of conflicts. Fix the conflicted files in your editor, then click "
     "\"Mark resolved and commit\" — or click \"Abort\" to go back to how things were."),
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
    if is_submodule(repo, path):
        from .submodules import discard_submodule  # submodules imports this module
        discard_submodule(repo, path)
    elif path in repo.untracked_files:
        _run(repo.git.clean, "-f", "-q", "--", path)
    else:
        _run(repo.git.restore, "--worktree", "--", path)


def is_submodule(repo: git.Repo, path: str) -> bool:
    """True if ``path`` is a submodule (a "gitlink") in the index."""
    out = repo.git.ls_files("-s", "--", path, with_exceptions=False)
    return out.startswith("160000 ")


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
class SubmoduleDiff:
    """What changed in a submodule: the commit it points to, and work inside it."""
    old: str | None = None  # short commit ids; None for a new / removed submodule
    new: str | None = None
    note: str = ""  # Git's note, e.g. "rewind", "new submodule", "commits not present"
    commits: list[tuple[str, str]] = field(default_factory=list)  # ("+" added / "-" removed, subject)
    modified: bool = False  # changed files inside
    untracked: bool = False  # untracked files inside


_SUBMODULE_LINE = re.compile(r"^Submodule (.+?) ([0-9a-f]+)\.\.\.?([0-9a-f]+)(?: \((.+?)\))?:?$")


def _parse_submodule_line(line: str, result) -> bool:
    """Lines from ``git diff --submodule=log``; True if the line was one of them."""
    if result.submodule is None and not line.startswith("Submodule "):
        return False
    sub = result.submodule or SubmoduleDiff()
    if line.startswith("Submodule "):
        m = _SUBMODULE_LINE.match(line)
        if m:
            sub.old = None if set(m.group(2)) == {"0"} else m.group(2)
            sub.new = None if set(m.group(3)) == {"0"} else m.group(3)
            sub.note = m.group(4) or ""
        elif line.endswith("contains modified content"):
            sub.modified = True
        elif line.endswith("contains untracked content"):
            sub.untracked = True
        else:
            return False
    elif line.startswith(("  > ", "  < ")):
        sub.commits.append(("+" if line[2] == ">" else "-", line[4:]))
    else:
        return False
    result.submodule = sub
    return True


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
    submodule: SubmoduleDiff | None = None

    @property
    def empty(self) -> bool:
        return (not self.binary and self.submodule is None
                and not any(l.kind in ("add", "del") for l in self.lines))


def diff_file(repo: git.Repo, path: str, staged: bool = False) -> FileDiff:
    """Unified diff of one file: staged (index vs HEAD) or unstaged (worktree vs index).

    Untracked files are shown as entirely added.
    """
    (path,) = check_paths([path])
    opts = ["--no-color", "--no-ext-diff", "--no-textconv", "--submodule=log"]
    if not staged and path in repo.untracked_files:
        raw = repo.git.diff(*opts, "--no-index", "--", os.devnull, path,
                            with_exceptions=False, stdout_as_string=False)
    elif staged:
        raw = _run(repo.git.diff, *opts, "--cached", "--", path, stdout_as_string=False)
    else:
        raw = _run(repo.git.diff, *opts, "--", path, stdout_as_string=False)
    result = parse_diff(raw.decode("utf-8", errors="replace"), path, staged)
    if not staged and (result.submodule is not None or is_submodule(repo, path)):
        _submodule_work(repo, path, result)
    return result


def _submodule_work(repo: git.Repo, path: str, result: FileDiff) -> None:
    """Changed / untracked files inside a submodule, from ``git status`` (git diff omits
    them when the commit moved too)."""
    sub = result.submodule or SubmoduleDiff()
    out = repo.git.status("--porcelain=v2", "-z", "--", path, with_exceptions=False)
    for entry in out.split("\0"):
        parts = entry.split(" ", 3)
        if len(parts) > 2 and parts[0] in ("1", "2") and parts[2].startswith("S"):
            sub.modified = sub.modified or parts[2][2] == "M"
            sub.untracked = sub.untracked or parts[2][3] == "U"
    result.submodule = sub


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
        elif not in_hunk and _parse_submodule_line(line, result):
            pass
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
    kwargs = {"env": env}
    if os.name != "nt":  # GitPython can't stop a stuck command on Windows
        kwargs["kill_after_timeout"] = NETWORK_TIMEOUT
    return kwargs


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
