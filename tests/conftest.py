from __future__ import annotations

import subprocess
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
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)  # gitlane's state goes to the temp home


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


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
