"""Open repos (tabs), recent repos and the folder browser (milestone 7).

The state is a small JSON file in the user's config folder
(``$XDG_CONFIG_HOME/gitlane/state.json``, usually ``~/.config/gitlane/``).
Closing a tab only removes it from this list; the repo is never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .git_ops import NotAGitRepo, open_repo

MAX_RECENT = 12
MAX_ENTRIES = 1000  # folders listed by the browser


def config_dir() -> Path:
    """Where gitlane keeps its state: $XDG_CONFIG_HOME, %APPDATA% on Windows, else ~/.config."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base and sys.platform == "win32":
        base = os.environ.get("APPDATA")
    return Path(base or Path.home() / ".config") / "gitlane"


def _is_full_path(path: Path) -> bool:
    """Absolute, or rooted like "/" or "\\" (on Windows that means the current drive)."""
    return path.is_absolute() or bool(path.root)


def repo_id(path: str | Path) -> str:
    """Stable id for a repo root (same path → same id, across restarts)."""
    return hashlib.sha1(str(path).encode("utf-8", "surrogateescape")).hexdigest()[:12]


def repo_root(path: str | Path) -> Path:
    """The top folder of the repo containing ``path`` (raises NotAGitRepo)."""
    path = Path(os.path.expanduser(str(path).strip()))
    if not str(path).strip():
        raise NotAGitRepo("Type or pick a folder first.")
    if not _is_full_path(path):
        raise NotAGitRepo(f"Use a full path, like {Path.home() / 'dev' / 'project'}")
    repo = open_repo(path)
    try:
        return Path(repo.working_tree_dir).resolve()
    finally:
        repo.close()


@dataclass
class Tab:
    id: str
    path: Path

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "path": str(self.path)}


class Workspace:
    """Thread-safe list of open tabs plus recent repos, saved on every change."""

    def __init__(self, state_file: Path | None = None):
        self.state_file = state_file or config_dir() / "state.json"
        self._lock = threading.Lock()
        self.tabs: list[Tab] = []
        self.active: str | None = None
        self.recent: list[Path] = []
        self.hidden: dict[str, list[str]] = {}  # repo path -> refs hidden in its graph
        self._load()

    # --- persistence ----------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for p in data.get("open", []):
            if isinstance(p, str) and _is_repo_root(Path(p)):
                self._add(Path(p))
        self.recent = [Path(p) for p in data.get("recent", []) if isinstance(p, str)][:MAX_RECENT]
        hidden = data.get("hidden")
        if isinstance(hidden, dict):
            self.hidden = {k: [r for r in v if isinstance(r, str)] for k, v in hidden.items()
                           if isinstance(k, str) and isinstance(v, list)}
        active = data.get("active")
        active = repo_id(active) if isinstance(active, str) else None
        if any(t.id == active for t in self.tabs):
            self.active = active
        elif self.tabs:
            self.active = self.tabs[0].id

    def _save(self) -> None:
        active = next((str(t.path) for t in self.tabs if t.id == self.active), None)
        data = {"open": [str(t.path) for t in self.tabs], "active": active,
                "recent": [str(p) for p in self.recent], "hidden": self.hidden}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.state_file)
        except OSError:
            pass  # remembering tabs is a convenience; never break the app over it

    # --- tabs ---------------------------------------------------------------------

    def _add(self, root: Path) -> Tab:
        rid = repo_id(root)
        for t in self.tabs:
            if t.id == rid:
                return t
        tab = Tab(rid, root)
        self.tabs.append(tab)
        return tab

    def open(self, path: str | Path) -> Tab:
        """Open (or switch to) the repo containing ``path`` and make it active."""
        root = repo_root(path)
        with self._lock:
            tab = self._add(root)
            self.active = tab.id
            self.recent = [root] + [p for p in self.recent if p != root]
            self.recent = self.recent[:MAX_RECENT]
            self._save()
            return tab

    def close(self, rid: str) -> None:
        with self._lock:
            i = next((i for i, t in enumerate(self.tabs) if t.id == rid), None)
            if i is None:
                return
            self.tabs.pop(i)
            if self.active == rid:
                self.active = self.tabs[min(i, len(self.tabs) - 1)].id if self.tabs else None
            self._save()

    def activate(self, rid: str) -> None:
        with self._lock:
            if any(t.id == rid for t in self.tabs):
                self.active = rid
                self._save()

    def path_of(self, rid: str | None) -> Path | None:
        return next((t.path for t in self.tabs if t.id == rid), None)

    def hidden_of(self, path: Path) -> set[str]:
        """Branches hidden in this repo's graph (full ref names)."""
        return set(self.hidden.get(str(path), ()))

    def set_hidden(self, path: Path, refs: set[str]) -> None:
        with self._lock:
            if refs:
                self.hidden[str(path)] = sorted(refs)
            else:
                self.hidden.pop(str(path), None)
            self._save()

    def recent_repos(self) -> list[Path]:
        """Recent repos that still exist and aren't open in a tab."""
        open_paths = {t.path for t in self.tabs}
        return [p for p in self.recent if p not in open_paths and _is_repo_root(p)]

    def as_dict(self) -> dict:
        return {"tabs": [t.as_dict() for t in self.tabs], "active": self.active}


def _is_repo_root(path: Path) -> bool:
    return path.is_absolute() and _has_git(path)


def _has_git(path: Path) -> bool:
    try:
        return (path / ".git").exists()
    except OSError:  # e.g. a folder we may not look into
        return False


# --- folder browser ---------------------------------------------------------------

@dataclass
class Folder:
    name: str
    path: Path
    is_repo: bool


@dataclass
class Listing:
    path: Path
    parent: Path | None
    folders: list[Folder]
    is_repo: bool
    truncated: bool = False


def browse(path: str | Path | None) -> Listing:
    """Sub-folders of ``path`` (default: home); hidden folders are skipped."""
    raw = str(path).strip() if path else ""
    folder = Path(os.path.expanduser(raw)) if raw else Path.home()
    if not _is_full_path(folder):
        raise NotAGitRepo("Use a full path.")
    folder = folder.resolve()
    if not folder.is_dir():
        raise NotAGitRepo(f"Folder does not exist: {folder}")
    folders: list[Folder] = []
    truncated = False
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError as e:
        raise NotAGitRepo(f"Can't open {folder}: {e.strerror}") from None
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if len(folders) >= MAX_ENTRIES:
            truncated = True
            break
        p = Path(entry.path)
        folders.append(Folder(entry.name, p, _has_git(p)))
    parent = folder.parent if folder.parent != folder else None
    return Listing(folder, parent, folders, _has_git(folder), truncated)
