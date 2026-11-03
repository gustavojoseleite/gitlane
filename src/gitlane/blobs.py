"""Reading file contents at a given version, used for image previews."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import git

from .git_ops import GitError, check_paths
from .graph import resolve_commit

MAX_BLOB_BYTES = 20 * 1024 * 1024

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}

# Special revisions besides commit ids.
WORKTREE = "WORKTREE"  # the file on disk
INDEX = "INDEX"  # the staged version
HEAD = "HEAD"  # the last commit


def image_type(path: str) -> str | None:
    return IMAGE_TYPES.get(PurePosixPath(path).suffix.lower())


def _spec(repo: git.Repo, rev: str, path: str) -> str:
    if rev == INDEX:
        return f":0:{path}"
    if rev == HEAD:
        return f"HEAD:{path}"
    return f"{resolve_commit(repo, rev)}:{path}"


def _worktree_file(repo: git.Repo, path: str) -> Path | None:
    root = Path(repo.working_tree_dir).resolve()
    f = (root / path).resolve()
    if not f.is_relative_to(root) or not f.is_file():  # also blocks symlinks pointing outside
        return None
    return f


def blob_exists(repo: git.Repo, rev: str, path: str) -> bool:
    (path,) = check_paths([path])
    if rev == WORKTREE:
        return _worktree_file(repo, path) is not None
    code, _, _ = repo.git.cat_file("-e", _spec(repo, rev, path),
                                   with_extended_output=True, with_exceptions=False)
    return code == 0


def read_blob(repo: git.Repo, rev: str, path: str) -> bytes | None:
    """Contents of ``path`` at ``rev``; None if the file does not exist there."""
    (path,) = check_paths([path])
    if rev == WORKTREE:
        f = _worktree_file(repo, path)
        if f is None:
            return None
        if f.stat().st_size > MAX_BLOB_BYTES:
            raise GitError("File is too large to preview.")
        return f.read_bytes()
    spec = _spec(repo, rev, path)
    code, size, _ = repo.git.cat_file("-s", spec, with_extended_output=True, with_exceptions=False)
    if code != 0:
        return None
    if int(size) > MAX_BLOB_BYTES:
        raise GitError("File is too large to preview.")
    return repo.git.cat_file("blob", spec, stdout_as_string=False, strip_newline_in_stdout=False)
