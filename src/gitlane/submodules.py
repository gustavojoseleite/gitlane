"""Submodules: list them, set them up / update them, and undo a moved submodule.

A submodule is another repository inside this one; the project records which
of its commits to use. gitlane shows that state and can bring a submodule back
to the recorded commit. Work *inside* a submodule happens in its own tab.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import git

from .git_ops import GitError, _network_kwargs, _run, check_paths, get_status

STATES = {
    " ": "up to date",
    "+": "on another commit",
    "-": "not set up",
    "U": "has conflicts",
}


@dataclass
class Submodule:
    path: str  # relative to the repo root, with "/"
    sha: str  # commit it is on (or the recorded one when not set up)
    state: str  # one of STATES values

    @property
    def ready(self) -> bool:
        """Checked out, so it can be opened in a tab."""
        return self.state != "not set up"


def list_submodules(repo: git.Repo) -> list[Submodule]:
    if not (Path(repo.working_tree_dir) / ".gitmodules").exists():
        return []
    code, out, _ = repo.git.submodule("status", with_extended_output=True, with_exceptions=False)
    if code != 0:
        return []
    subs = []
    for line in out.splitlines():
        if len(line) < 3:
            continue
        flag, rest = line[0], line[1:]
        sha, _, tail = rest.partition(" ")
        # "path (describe)" -- the part in brackets is only there when the submodule is checked out
        path = tail.rpartition(" (")[0] if tail.endswith(")") and " (" in tail else tail
        subs.append(Submodule(path=path, sha=sha, state=STATES.get(flag, "up to date")))
    return subs


def _require(repo: git.Repo, path: str) -> Submodule:
    (path,) = check_paths([path])
    sub = next((s for s in list_submodules(repo) if s.path == path), None)
    if sub is None:
        raise GitError(f"Submodule '{path}' not found.")
    return sub


def update(repo: git.Repo, path: str | None = None) -> str:
    """Set up (if needed) and check out the commit this project records.

    Commits made inside the submodule are not deleted; they stay in its history.
    """
    paths = [_require(repo, path).path] if path else []
    if not path and not list_submodules(repo):
        raise GitError("This repository has no submodules.")
    _run(repo.git.submodule, "update", "--init", "--recursive", "--", *paths, **_network_kwargs(repo))
    if path:
        return f"{path} is on the commit this project records."
    return "All submodules are on the commits this project records."


def discard_submodule(repo: git.Repo, path: str) -> None:
    """"Discard" for a submodule: move it back to the recorded commit.

    Files changed inside it are never thrown away from here; Git stops if
    they would be overwritten.
    """
    change = next((c for c in get_status(repo).unstaged if c.path == path), None)
    note = (change.submodule or "") if change else ""
    if "new commits" not in note:
        raise GitError(f"The changes are inside {path}, which is a separate repository.",
                       hint=f"Open {path} in its own tab to see or discard them.",
                       action={"label": f"Open {path}", "open": str(Path(repo.working_tree_dir) / path)})
    _run(repo.git.submodule, "update", "--init", "--", path, **_network_kwargs(repo))
