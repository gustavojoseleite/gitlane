from __future__ import annotations

import subprocess
import time

import pytest

from gitlane import graph
from gitlane.git_ops import GitError, open_repo
from gitlane.graph import Commit, Ref, layout, parse_refs

from .conftest import run


def C(sha, *parents):
    return Commit(sha, list(parents), "a", "a@x", 0, [], sha)


def commit(path, name, msg=None):
    (path / name).write_text(name)
    run(path, "add", name)
    run(path, "commit", "-q", "-m", msg or name)
    return run(path, "rev-parse", "HEAD")


@pytest.fixture
def repo_at(make_repo):
    opened = []

    def _open(path):
        r = open_repo(path)
        opened.append(r)
        return r

    yield _open
    for r in opened:
        r.close()


# --- layout (pure) -----------------------------------------------------------

def test_linear_history_single_lane():
    rows = layout([C("c3", "c2"), C("c2", "c1"), C("c1")])
    assert [r.col for r in rows] == [0, 0, 0]
    assert rows[0].top == []  # tip: nothing above
    assert rows[1].top == [(0, 0, 0)] and rows[1].bottom == [(0, 0, 0)]
    assert rows[2].bottom == []  # root: nothing below


def test_merge_and_branch_lanes():
    #   m2 = merge(m1, f1); f1 and m1 both come from r
    rows = layout([C("m2", "m1", "f1"), C("f1", "r"), C("m1", "r"), C("r")])
    m2, f1, m1, r = rows
    assert m2.bottom == [(0, 0, 0), (0, 1, 1)]  # line to second parent in lane 1
    assert f1.col == 1 and (0, 0, 0) in f1.top  # main lane passes through
    assert m1.col == 0 and (1, 1, 1) in m1.bottom
    assert sorted(r.top) == [(0, 0, 0), (1, 0, 1)]  # feature lane joins the root
    assert all(row.width <= 2 for row in rows)


def test_two_tips_sharing_a_parent():
    rows = layout([C("a", "base"), C("b", "base"), C("base")])
    assert rows[0].col == 0 and rows[1].col == 1
    assert rows[1].top == [(0, 0, 0)]  # b is a tip: no line above it in lane 1
    assert sorted(rows[2].top) == [(0, 0, 0), (1, 0, 1)]


def test_lanes_are_reused():
    # Two short side branches one after another should reuse lane 1.
    rows = layout([
        C("m3", "m2", "y"), C("y", "m2"), C("m2", "m1", "x"), C("x", "m1"), C("m1"),
    ])
    assert max(r.width for r in rows) == 2


def test_unrelated_histories():
    rows = layout([C("a2", "a1"), C("b1"), C("a1")])
    assert rows[1].col == 1 and rows[1].top == [(0, 0, 0)] and rows[1].bottom == [(0, 0, 0)]


def test_octopus_merge():
    rows = layout([C("m", "p1", "p2", "p3"), C("p3"), C("p2"), C("p1")])
    assert [b for b in rows[0].bottom] == [(0, 0, 0), (0, 1, 1), (0, 2, 2)]


# --- refs parsing --------------------------------------------------------------

def test_parse_refs():
    refs = parse_refs("HEAD -> main, origin/main, origin/HEAD, tag: v1.0, feature", ["origin"])
    assert refs == [
        Ref("head", "main"), Ref("remote", "origin/main"),
        Ref("tag", "v1.0"), Ref("branch", "feature"),
    ]
    assert parse_refs("HEAD", []) == [Ref("detached", "HEAD")]
    assert parse_refs("", ["origin"]) == []


def test_branch_with_slash_is_local_when_not_a_remote():
    assert parse_refs("feature/login", ["origin"]) == [Ref("branch", "feature/login")]


# --- reading a real repo ------------------------------------------------------------

def test_graph_of_real_repo(make_repo, repo_at):
    path = make_repo()
    run(path, "checkout", "-q", "-b", "feature")
    commit(path, "f.txt")
    run(path, "checkout", "-q", "main")
    commit(path, "m.txt")
    run(path, "merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    run(path, "tag", "v1")
    page = graph.graph_page(repo_at(path))
    subjects = [r.commit.subject for r in page.rows]
    assert subjects[0] == "Merge feature" and subjects[-1] == "Initial commit"
    assert len(page.rows) == 4 and not page.has_more
    top = page.rows[0]
    assert Ref("head", "main") in top.commit.refs and Ref("tag", "v1") in top.commit.refs
    assert len(top.commit.parents) == 2
    assert page.width == 2


def test_stash_shown_above_its_base(make_repo, repo_at):
    """Only the stash itself appears, not Git's internal index/untracked commits."""
    path = make_repo()
    base = commit(path, "a.txt", "base")
    (path / "a.txt").write_text("wip\n")
    (path / "new.txt").write_text("untracked\n")
    run(path, "stash", "-q", "-u", "-m", "half done")
    commit(path, "b.txt", "after")
    page = graph.graph_page(repo_at(path))
    assert [r.commit.subject for r in page.rows] == ["after", "half done", "base", "Initial commit"]
    st = page.rows[1]
    assert st.commit.stash == 0 and st.commit.parents == [base]
    assert st.commit.refs == [Ref("stash", "stash@{0}")]
    assert st.col != page.rows[0].col  # its own short lane into the base commit
    assert (st.col, page.rows[2].col) in [(a, b) for a, b, _ in page.rows[2].top]


def test_several_stashes_newest_first(make_repo, repo_at):
    path = make_repo()
    for msg in ("first", "second"):
        (path / "README.md").write_text(msg)
        run(path, "stash", "-q", "-m", msg)
    rows = graph.graph_page(repo_at(path)).rows
    assert [(r.commit.subject, r.commit.stash) for r in rows] == \
           [("second", 0), ("first", 1), ("Initial commit", None)]


def test_stash_paging(make_repo, repo_at):
    path = make_repo()
    for i in range(4):
        commit(path, f"{i}.txt")
    (path / "README.md").write_text("wip")
    run(path, "stash", "-q")
    r = repo_at(path)
    full = graph.graph_page(r, 0, 100).rows
    p1, p2 = graph.graph_page(r, 0, 3), graph.graph_page(r, 3, 3)
    assert p1.has_more and not p2.has_more
    assert [x.commit.sha for x in p1.rows + p2.rows] == [x.commit.sha for x in full]
    assert len(full) == 6


def test_dropping_older_stash_changes_refs_version(make_repo, repo_at):
    path = make_repo()
    r = repo_at(path)
    for msg in ("first", "second"):
        (path / "README.md").write_text(msg)
        run(path, "stash", "-q", "-m", msg)
    v1 = graph.refs_version(r)
    run(path, "stash", "drop", "-q", "stash@{1}")  # refs/stash itself doesn't move
    assert graph.refs_version(r) != v1


def test_detached_commit_included(make_repo, repo_at):
    path = make_repo()
    run(path, "checkout", "-q", "--detach")
    commit(path, "lost.txt", "only on detached HEAD")
    page = graph.graph_page(repo_at(path))
    assert page.rows[0].commit.subject == "only on detached HEAD"
    assert page.rows[0].commit.refs == [Ref("detached", "HEAD")]


def test_empty_repo(make_repo, repo_at):
    page = graph.graph_page(repo_at(make_repo(commit=False)))
    assert page.rows == [] and not page.has_more


def test_paging_matches_full_layout(make_repo, repo_at):
    path = make_repo()
    for i in range(6):
        commit(path, f"{i}.txt")
    r = repo_at(path)
    full = graph.graph_page(r, 0, 100).rows
    p1, p2, p3 = graph.graph_page(r, 0, 3), graph.graph_page(r, 3, 3), graph.graph_page(r, 6, 3)
    assert p1.has_more and p2.has_more and not p3.has_more
    paged = p1.rows + p2.rows + p3.rows
    assert [(x.commit.sha, x.col, x.top, x.bottom) for x in paged] == \
           [(x.commit.sha, x.col, x.top, x.bottom) for x in full]


def test_refs_version_changes(make_repo, repo_at):
    path = make_repo()
    r = repo_at(path)
    v1 = graph.refs_version(r)
    assert graph.refs_version(r) == v1
    commit(path, "x.txt")
    v2 = graph.refs_version(r)
    run(path, "checkout", "-q", "-b", "other")
    v3 = graph.refs_version(r)
    assert len({v1, v2, v3}) == 3


# --- commit details ----------------------------------------------------------------

def test_commit_detail(make_repo, repo_at):
    path = make_repo()
    (path / "README.md").write_text("changed\n")
    (path / "new.txt").write_text("n")
    run(path, "add", "-A")
    run(path, "commit", "-q", "-m", "Subject line", "-m", "Body paragraph.")
    sha = run(path, "rev-parse", "HEAD")
    d = graph.commit_detail(repo_at(path), sha[:8])
    assert d.commit.sha == sha and d.commit.subject == "Subject line"
    assert d.body == "Body paragraph."
    assert {(f.path, f.status) for f in d.files} == {("README.md", "modified"), ("new.txt", "added")}


def test_root_commit_detail(make_repo, repo_at):
    path = make_repo()
    d = graph.commit_detail(repo_at(path), run(path, "rev-parse", "HEAD"))
    assert d.compared_to is None
    assert [(f.path, f.status) for f in d.files] == [("README.md", "added")]


def test_rename_in_commit(make_repo, repo_at):
    path = make_repo()
    run(path, "mv", "README.md", "DOCS.md")
    run(path, "commit", "-q", "-m", "rename")
    r = repo_at(path)
    d = graph.commit_detail(r, run(path, "rev-parse", "HEAD"))
    assert [(f.path, f.status, f.orig_path) for f in d.files] == [("DOCS.md", "renamed", "README.md")]
    diff = graph.commit_file_diff(r, d.commit.sha, "DOCS.md", "README.md")
    assert diff.empty  # pure rename, no content change


def test_merge_commit_files_vs_first_parent(make_repo, repo_at):
    path = make_repo()
    run(path, "checkout", "-q", "-b", "feature")
    commit(path, "feature.txt")
    run(path, "checkout", "-q", "main")
    commit(path, "main.txt")
    run(path, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    d = graph.commit_detail(repo_at(path), run(path, "rev-parse", "HEAD"))
    assert [f.path for f in d.files] == ["feature.txt"]
    assert len(d.commit.parents) == 2


def test_commit_file_diff(make_repo, repo_at):
    path = make_repo()
    (path / "README.md").write_text("hello\nworld\n")
    run(path, "commit", "-q", "-am", "add world")
    r = repo_at(path)
    d = graph.commit_file_diff(r, run(path, "rev-parse", "HEAD"), "README.md")
    assert [l.text for l in d.lines if l.kind == "add"] == ["world"]
    root = graph.commit_file_diff(r, run(path, "rev-parse", "HEAD~1"), "README.md")
    assert [l.text for l in root.lines if l.kind == "add"] == ["hello"]


@pytest.mark.parametrize("bad", ["", "xyz", "HEAD", "--all", "abc", "0" * 40, "a" * 65])
def test_bad_commit_ids_rejected(make_repo, repo_at, bad):
    r = repo_at(make_repo())
    with pytest.raises(GitError):
        graph.commit_detail(r, bad)


def test_commit_diff_rejects_outside_path(make_repo, repo_at):
    path = make_repo()
    with pytest.raises(GitError):
        graph.commit_file_diff(repo_at(path), run(path, "rev-parse", "HEAD"), "../../etc/passwd")


# --- performance (SPEC: responsive with ~10,000 commits) ---------------------------

def make_big_repo(path, n=10_000):
    """Build n commits quickly with git fast-import, with a side branch every 50."""
    path.mkdir()
    run(path, "init", "-q")
    lines = []
    mark = 0
    main_mark = None

    def emit(ref, msg, ts, parent=None, merge=None):
        nonlocal mark
        mark += 1
        data = msg.encode()
        lines.extend([
            f"commit {ref}".encode(), f"mark :{mark}".encode(),
            f"committer T <t@x> {ts} +0000".encode(), f"data {len(data)}".encode(), data,
        ])
        if parent:
            lines.append(f"from :{parent}".encode())
        if merge:
            lines.append(f"merge :{merge}".encode())
        lines.append(b"")
        return mark

    i = 0
    while i < n:
        ts = 1_700_000_000 + i
        if i % 50 == 49 and main_mark:
            # A side commit branched from main, then merged back: a real 2-lane shape.
            side = emit("refs/heads/side", f"side {i}", ts, parent=main_mark)
            i += 1
            if i >= n:
                break
            main_mark = emit("refs/heads/main", f"merge {i}", ts + 1, parent=main_mark, merge=side)
        else:
            main_mark = emit("refs/heads/main", f"commit {i}", ts, parent=main_mark)
        i += 1
    subprocess.run(["git", "fast-import", "--quiet"], cwd=path, input=b"\n".join(lines) + b"\n", check=True)
    run(path, "reset", "-q", "--hard", "main")
    return path


def test_large_repo_pages_are_fast(tmp_path, repo_at):
    r = repo_at(make_big_repo(tmp_path / "big"))
    t0 = time.perf_counter()
    first = graph.graph_page(r, 0)
    t1 = time.perf_counter()
    last = graph.graph_page(r, 9_900)
    t2 = time.perf_counter()
    assert len(first.rows) == graph.PAGE_SIZE and first.has_more
    assert len(last.rows) == 100 and not last.has_more
    # Generous limits so slow CI machines don't flake; typically far lower.
    assert t1 - t0 < 2, f"first page took {t1 - t0:.2f}s"
    assert t2 - t1 < 5, f"last page took {t2 - t1:.2f}s"


# --- local / remote / both -----------------------------------------------------------

def test_merge_tracking_rules():
    from gitlane.graph import merge_tracking
    refs = [Ref("head", "main"), Ref("remote", "origin/main"), Ref("remote", "origin/x"),
            Ref("branch", "y"), Ref("remote", "origin/y"), Ref("tag", "v1")]
    out = merge_tracking(refs, {"main": "origin/main", "x": "origin/x"})
    assert [(r.kind, r.name, r.remote) for r in out] == [
        ("head", "main", "origin/main"),  # tracks it, same commit -> one label
        ("remote", "origin/x", None),     # x itself isn't on this commit
        ("branch", "y", None),            # same name, but y doesn't track origin/y
        ("remote", "origin/y", None),
        ("tag", "v1", None),
    ]


def test_graph_shows_both_or_separate(make_repo, repo_at, tmp_path):
    path = make_repo()
    run(tmp_path, "init", "-q", "--bare", "server.git")
    run(path, "remote", "add", "origin", str(tmp_path / "server.git"))
    run(path, "push", "-q", "-u", "origin", "main")
    page = graph.graph_page(repo_at(path))
    assert page.rows[0].commit.refs == [Ref("head", "main", remote="origin/main")]
    commit(path, "new.txt")  # main moves ahead of origin/main
    rows = graph.graph_page(repo_at(path)).rows
    assert rows[0].commit.refs == [Ref("head", "main")]
    assert rows[1].commit.refs == [Ref("remote", "origin/main")]
    assert graph.commit_detail(repo_at(path), rows[1].commit.sha).commit.refs == [Ref("remote", "origin/main")]
