"""Tags: list, create, delete, push and delete on the remote.

Every function returns a short message for the UI or raises GitError.
"""

from __future__ import annotations

from dataclasses import dataclass

import git

from .git_ops import GitError, _has_head, _network_kwargs, _run
from .graph import resolve_commit


@dataclass
class Tag:
    name: str
    sha: str  # the commit it points to
    annotated: bool = False
    message: str = ""  # first line of an annotated tag's message


def list_tags(repo: git.Repo) -> list[Tag]:
    """Tags on commits, newest first (tags on trees or blobs are left out)."""
    out = repo.git.for_each_ref(
        "--sort=-creatordate",
        "--format=%(refname)%00%(objecttype)%00%(objectname)%00%(*objecttype)%00%(*objectname)"
        "%00%(contents:subject)",
        "refs/tags",
    )
    tags = []
    for line in out.splitlines():
        ref, kind, sha, peeled_kind, peeled, subject = (line.split("\0") + [""] * 6)[:6]
        name = ref[len("refs/tags/"):]
        if kind == "commit":
            tags.append(Tag(name, sha))
        elif kind == "tag" and peeled_kind == "commit":
            tags.append(Tag(name, peeled, annotated=True, message=subject))
    return tags


def check_tag_name(repo: git.Repo, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise GitError("Type a name for the tag.")
    code, _, _ = repo.git.check_ref_format(f"refs/tags/{name}",
                                           with_extended_output=True, with_exceptions=False)
    if code != 0 or name.startswith("-") or name == "HEAD":
        raise GitError(f"'{name}' is not a valid tag name.",
                       hint="Tag names can't contain spaces, '..', '~', '^', ':', '?', '*', '[' "
                            "or start with '-'. Try something like v1.2.0.")
    return name


def _tag_names(repo: git.Repo) -> set[str]:
    return {t.name for t in list_tags(repo)}


def _require_tag(repo: git.Repo, name: str) -> str:
    if name not in _tag_names(repo):
        raise GitError(f"Tag '{name}' not found.")
    return name


def create_tag(repo: git.Repo, name: str, sha: str, message: str = "") -> str:
    """Annotated tag when there is a message, lightweight otherwise."""
    name = check_tag_name(repo, name)
    if not _has_head(repo):
        raise GitError("Make a first commit before creating tags.")
    sha = resolve_commit(repo, sha)
    code, _, _ = repo.git.show_ref("--verify", "-q", f"refs/tags/{name}",
                                   with_extended_output=True, with_exceptions=False)
    if code == 0:
        raise GitError(f"A tag named {name} already exists.",
                       hint="Pick another name, or delete the old tag first.")
    message = (message or "").strip()
    if message:
        _run(repo.git.tag, "-a", name, "-m", message, sha)
    else:
        _run(repo.git.tag, name, sha)
    return f"Created tag {name} at {sha[:7]}."


def delete_tag(repo: git.Repo, name: str) -> str:
    _require_tag(repo, name)
    _run(repo.git.tag, "-d", name)
    return f"Deleted tag {name} (on your computer only)."


def _tag_remote(repo: git.Repo) -> str:
    names = [r.name for r in repo.remotes]
    if not names:
        raise GitError("This repository has no remote. Add one with "
                       "`git remote add origin <url>`.")
    if "origin" in names:
        return "origin"
    if len(names) == 1:
        return names[0]
    raise GitError("This repository has several remotes but none called 'origin', so gitlane "
                   "doesn't know where to send the tag. Use `git push <remote> <tag>` in a terminal.")


def push_tag(repo: git.Repo, name: str) -> str:
    _require_tag(repo, name)
    remote = _tag_remote(repo)
    try:
        _run(repo.git.push, remote, f"refs/tags/{name}:refs/tags/{name}", **_network_kwargs(repo))
    except GitError as e:
        if "already exists" in str(e):
            e.hint = (f"{remote} already has a different tag called {name}. Tags are not "
                      "replaced on push; delete the tag on the remote first if you really want to move it.")
        raise
    return f"Pushed tag {name} to {remote}."


def delete_remote_tag(repo: git.Repo, name: str) -> str:
    """Delete a tag on the remote server; the local tag (if any) is kept."""
    name = check_tag_name(repo, name)
    remote = _tag_remote(repo)
    # Git "deletes" a missing full ref name without an error, so ask the server first.
    if not _run(repo.git.ls_remote, "--tags", remote, f"refs/tags/{name}", **_network_kwargs(repo)).strip():
        return f"Tag {name} is not on {remote}, so there was nothing to delete."
    _run(repo.git.push, remote, "--delete", f"refs/tags/{name}", **_network_kwargs(repo))
    kept = " Your local tag is kept." if name in _tag_names(repo) else ""
    return f"Deleted tag {name} on {remote}.{kept}"
