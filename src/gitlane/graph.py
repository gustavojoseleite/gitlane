"""Commit graph: reading history and computing the lane layout.

The layout is computed row by row, top (newest) to bottom. Each row says
where its commit's dot sits (``col``) and which line segments cross it:

* ``top``: segments from the top edge of the row to its middle
* ``bottom``: segments from the middle to the bottom edge

Each segment is ``(from_col, to_col, color)``. A straight line through
the row is ``(i, i, c)`` in both lists; a branch joining the dot is
``(i, col, c)`` in ``top``; a line to a second parent is ``(col, j, c)``
in ``bottom``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

import git

from .git_ops import FileChange, FileDiff, GitError, _CODES, _has_head, _run, parse_diff

PAGE_SIZE = 300

_FIELD = "\x1f"
_RECORD = "\x1e"
_LOG_FORMAT = _RECORD + _FIELD.join(["%H", "%P", "%an", "%ae", "%at", "%D", "%s"])


@dataclass
class Ref:
    kind: str  # "head" (current branch), "branch", "remote", "tag", "detached"
    name: str


@dataclass
class Commit:
    sha: str
    parents: list[str]
    author: str
    email: str
    timestamp: int
    refs: list[Ref]
    subject: str
    stash: int | None = None  # stash@{N} position for stash rows, else None

    @property
    def date(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M")


Segment = tuple[int, int, int]


@dataclass
class Row:
    commit: Commit
    col: int
    color: int
    top: list[Segment] = field(default_factory=list)
    bottom: list[Segment] = field(default_factory=list)

    @property
    def width(self) -> int:
        cols = [self.col] + [c for seg in self.top + self.bottom for c in seg[:2]]
        return max(cols) + 1


# --- reading history --------------------------------------------------------

def parse_refs(decoration: str, remotes: list[str]) -> list[Ref]:
    """Turn ``%D`` output ("HEAD -> main, origin/main, tag: v1") into Refs."""
    refs = []
    for part in filter(None, (p.strip() for p in decoration.split(","))):
        if part.startswith("HEAD -> "):
            refs.append(Ref("head", part[len("HEAD -> "):]))
        elif part == "HEAD":
            refs.append(Ref("detached", "HEAD"))
        elif part.startswith("tag: "):
            refs.append(Ref("tag", part[len("tag: "):]))
        elif any(part.startswith(r + "/") for r in remotes):
            if not part.endswith("/HEAD"):  # origin/HEAD is noise
                refs.append(Ref("remote", part))
        else:
            refs.append(Ref("branch", part))
    return refs


def read_commits(repo: git.Repo, limit: int) -> list[Commit]:
    """Newest-first history of all branches, tags and HEAD (stash excluded)."""
    if not _has_head(repo) and not repo.refs:
        return []
    raw = _run(
        repo.git.log,
        "--exclude=refs/stash", "--all", "--topo-order",
        f"--max-count={limit}", f"--format={_LOG_FORMAT}", "--decorate=short",
        stdout_as_string=False,
    ).decode("utf-8", errors="replace")
    remotes = [r.name for r in repo.remotes]
    commits = []
    for record in raw.split(_RECORD)[1:]:
        sha, parents, author, email, ts, deco, subject = record.rstrip("\n").split(_FIELD, 6)
        commits.append(Commit(
            sha=sha, parents=parents.split(), author=author, email=email,
            timestamp=int(ts), refs=parse_refs(deco, remotes), subject=subject,
        ))
    return commits


def read_stashes(repo: git.Repo) -> list[Commit]:
    """Stashes as graph rows whose only parent is the commit they were made on."""
    from .stash import Stash  # stash imports this module

    code, out, _ = repo.git.stash(
        "list", f"--format={_FIELD.join(['%H', '%P', '%gs', '%ct', '%an', '%ae'])}",
        with_extended_output=True, with_exceptions=False,
    )
    if code != 0:
        return []
    rows = []
    for i, line in enumerate(out.splitlines()):
        sha, parents, message, ts, author, email = line.split(_FIELD, 5)
        st = Stash(index=i, sha=sha, message=message, timestamp=int(ts))
        rows.append(Commit(
            sha=sha, parents=parents.split()[:1], author=author, email=email, timestamp=int(ts),
            refs=[Ref("stash", st.ref)], subject=st.title, stash=i,
        ))
    return rows


def insert_stashes(commits: list[Commit], stashes: list[Commit]) -> list[Commit]:
    """Put each stash right above its base commit (newest stash first).

    Stashes whose base isn't in ``commits`` are left out; they show up once
    the page containing their base is loaded.
    """
    by_base: dict[str, list[Commit]] = {}
    for st in stashes:
        if st.parents:
            by_base.setdefault(st.parents[0], []).append(st)
    if not by_base:
        return commits
    out = []
    for c in commits:
        out.extend(by_base.get(c.sha, ()))
        out.append(c)
    return out


# --- layout -----------------------------------------------------------------

def layout(commits: list[Commit]) -> list[Row]:
    lanes: list[str | None] = []  # sha each column is waiting for
    colors: list[int] = []
    next_color = 0
    rows = []

    def free_slot(exclude: int = -1) -> int:
        for i, s in enumerate(lanes):
            if s is None and i != exclude:
                return i
        lanes.append(None)
        colors.append(0)
        return len(lanes) - 1

    for c in commits:
        tip = c.sha not in lanes
        if not tip:
            col = lanes.index(c.sha)
        else:  # a branch tip: start a new lane (no line above the dot)
            col = free_slot()
            lanes[col] = c.sha
            colors[col] = next_color
            next_color += 1

        row = Row(commit=c, col=col, color=colors[col])
        passing = []
        for i, s in enumerate(lanes):
            if s is None:
                continue
            if s == c.sha:
                if not (tip and i == col):
                    row.top.append((i, col, colors[i]))
                if i != col:
                    lanes[i] = None  # this lane ends here, merged into the dot
            else:
                row.top.append((i, i, colors[i]))
                passing.append(i)

        if c.parents:
            lanes[col] = c.parents[0]
            row.bottom.append((col, col, colors[col]))
            for p in c.parents[1:]:
                if p in lanes:
                    j = lanes.index(p)
                else:
                    j = free_slot(exclude=col)
                    lanes[j] = p
                    colors[j] = next_color
                    next_color += 1
                row.bottom.append((col, j, colors[j]))
        else:
            lanes[col] = None  # root commit: the line stops

        row.bottom.extend((i, i, colors[i]) for i in passing)
        while lanes and lanes[-1] is None:
            lanes.pop()
            colors.pop()
        rows.append(row)
    return rows


@dataclass
class GraphPage:
    rows: list[Row]
    skip: int
    has_more: bool

    @property
    def width(self) -> int:
        return max((r.width for r in self.rows), default=1)


def graph_page(repo: git.Repo, skip: int = 0, limit: int = PAGE_SIZE) -> GraphPage:
    """One page of the laid-out graph.

    The layout depends on every row above, so we lay out from the top each
    time; this stays fast for repositories with tens of thousands of commits.
    """
    skip = max(0, skip)
    commits = insert_stashes(read_commits(repo, skip + limit + 1), read_stashes(repo))
    rows = layout(commits[: skip + limit])
    return GraphPage(rows=rows[skip:], skip=skip, has_more=len(commits) > skip + limit)


def refs_version(repo: git.Repo) -> str:
    """Changes whenever any branch, tag or HEAD moves (used to refresh the graph)."""
    _, refs, _ = repo.git.show_ref("--head", with_extended_output=True, with_exceptions=False)
    _, head, _ = repo.git.symbolic_ref("-q", "HEAD", with_extended_output=True, with_exceptions=False)
    _, stashes, _ = repo.git.stash("list", "--format=%H", with_extended_output=True,
                                   with_exceptions=False)  # dropping stash@{1} keeps refs/stash
    return hashlib.sha1(f"{head}\n{refs}\n{stashes}".encode()).hexdigest()


# --- commit details ---------------------------------------------------------

_SHA = re.compile(r"[0-9a-f]{4,64}")


def resolve_commit(repo: git.Repo, sha: str) -> str:
    if not _SHA.fullmatch(sha or ""):
        raise GitError("Invalid commit id.")
    code, out, _ = repo.git.rev_parse(
        "--verify", "--quiet", f"{sha}^{{commit}}", with_extended_output=True, with_exceptions=False,
    )
    if code != 0:
        raise GitError(f"Commit {sha} not found.")
    return out.strip()


@dataclass
class CommitDetail:
    commit: Commit
    body: str
    files: list[FileChange]
    compared_to: str | None  # first parent the file list is relative to


def commit_detail(repo: git.Repo, sha: str) -> CommitDetail:
    sha = resolve_commit(repo, sha)
    raw = repo.git.show(
        "-s", "--decorate=short",
        f"--format={_FIELD.join(['%H', '%P', '%an', '%ae', '%at', '%D', '%s', '%b'])}", sha,
        stdout_as_string=False,
    ).decode("utf-8", errors="replace")
    h, parents, author, email, ts, deco, subject, body = raw.split(_FIELD, 7)
    commit = Commit(
        sha=h, parents=parents.split(), author=author, email=email, timestamp=int(ts),
        refs=parse_refs(deco, [r.name for r in repo.remotes]), subject=subject,
    )
    base = commit.parents[0] if commit.parents else None
    if base:
        out = repo.git.diff("--name-status", "-z", "-M", "--no-ext-diff", base, sha)
    else:
        out = repo.git.diff_tree("--root", "-r", "--name-status", "-z", "-M", "--no-commit-id", sha)
    return CommitDetail(commit=commit, body=body.strip(), files=_parse_name_status(out), compared_to=base)


def _parse_name_status(out: str) -> list[FileChange]:
    parts = out.split("\0")
    files = []
    i = 0
    while i < len(parts) and parts[i]:
        code = parts[i]
        if code[0] in "RC":
            files.append(FileChange(parts[i + 2], _CODES[code[0]], orig_path=parts[i + 1]))
            i += 3
        else:
            files.append(FileChange(parts[i + 1], _CODES.get(code[0], code[0])))
            i += 2
    return files


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def commit_file_diff(repo: git.Repo, sha: str, path: str, orig: str | None = None) -> FileDiff:
    from .git_ops import check_paths

    sha = resolve_commit(repo, sha)
    paths = check_paths([p for p in (orig, path) if p])
    parents = repo.commit(sha).parents
    base = parents[0].hexsha if parents else EMPTY_TREE
    raw = _run(repo.git.diff, "--no-color", "--no-ext-diff", "--no-textconv", "-M",
               base, sha, "--", *paths, stdout_as_string=False)
    return parse_diff(raw.decode("utf-8", errors="replace"), path, staged=False)
