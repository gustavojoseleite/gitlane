from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    """Isolate tests from the user's global Git config."""
    home = tmp_path_factory.mktemp("home")
    cfg = home / ".gitconfig"
    cfg.write_text(
        "[user]\n\tname = Test User\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n"
    )
    set_home(monkeypatch, home)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)  # gitlane's state goes to the temp home


def set_home(monkeypatch, home: Path) -> None:
    """Point the home folder at ``home`` on every OS (Windows reads USERPROFILE/APPDATA)."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))


def run(cwd: Path, *args: str) -> str:
    # Git prints UTF-8; don't let Windows decode it with its local code page.
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def rmtree(path: Path) -> None:
    """Delete a folder, including Git's read-only object files (Windows refuses otherwise)."""
    import os
    import shutil
    import stat

    def unlock(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=unlock)
    else:
        shutil.rmtree(path, onerror=unlock)


@pytest.fixture
def make_repo(tmp_path):
    """Create a non-bare repo; ``commit=True`` adds one initial commit."""

    def _make(name: str = "repo", commit: bool = True) -> Path:
        path = tmp_path / name
        path.mkdir()
        run(path, "init", "-q")
        if commit:
            (path / "README.md").write_text("hello\n")
            run(path, "add", "README.md")
            run(path, "commit", "-q", "-m", "Initial commit")
        return path

    return _make
