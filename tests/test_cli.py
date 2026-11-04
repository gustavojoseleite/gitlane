from __future__ import annotations

import os
import socket

import pytest

from gitlane import cli


def test_not_a_repo_exits_with_message(tmp_path, capsys):
    assert cli.main([str(tmp_path), "--no-browser"]) == 1
    err = capsys.readouterr().err
    assert "Not a Git repository" in err


def test_find_free_port_is_bindable_on_localhost():
    port = cli.find_free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_host_is_localhost_only():
    assert cli.HOST == "127.0.0.1"


def test_defaults():
    args = cli.parse_args([])
    assert args.path is None and args.port is None and not args.no_browser


def _capture_start(monkeypatch):
    """Run main() without serving: record which repo it would open."""
    import uvicorn

    import gitlane.server

    seen = {}
    real = gitlane.server.create_app

    def fake_create_app(repo_root, token, port, *a, **kw):
        seen["repo"] = repo_root
        return real(repo_root, token, port, *a, **kw)

    monkeypatch.setattr(gitlane.server, "create_app", fake_create_app)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self: None)
    return seen


def test_no_path_outside_repo_starts_with_picker(tmp_path, monkeypatch):
    seen = _capture_start(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--no-browser"]) == 0
    assert seen["repo"] is None


def test_no_path_inside_repo_opens_it(make_repo, monkeypatch):
    path = make_repo()
    (path / "sub").mkdir()
    seen = _capture_start(monkeypatch)
    monkeypatch.chdir(path / "sub")
    assert cli.main(["--no-browser"]) == 0
    assert seen["repo"] == path.resolve()


# --- startup checks ------------------------------------------------------------------

def test_missing_git_is_explained(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["--no-browser"]) == 1
    err = capsys.readouterr().err
    assert "Git is not installed" in err and "git-scm.com" in err


def test_old_git_is_explained(monkeypatch, capsys):
    class Done:
        stdout = "git version 2.20.1\n"
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: Done())
    assert cli.main(["--no-browser"]) == 1
    assert "too old" in capsys.readouterr().err


def test_current_git_passes():
    assert cli.check_git() is None


def test_python_dash_m_gitlane():
    import subprocess
    import sys

    from gitlane import __version__
    out = subprocess.run([sys.executable, "-m", "gitlane", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == f"gitlane {__version__}"


# --- app-menu launcher ----------------------------------------------------------------

def test_install_and_remove_launcher(monkeypatch, tmp_path, capsys):
    import sys

    from gitlane import launcher
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert cli.main(["--install-launcher"]) == 0
    entry = (tmp_path / "share" / "applications" / "gitlane.desktop").read_text()
    assert "Exec=" in entry and "-m gitlane" in entry and sys.executable in entry
    assert "Terminal=true" in entry and "Name=gitlane" in entry
    icon = launcher.icon_file()
    assert icon.read_text().startswith("<svg") and f"Icon={icon}" in entry
    assert "app menu" in capsys.readouterr().out
    assert cli.main(["--remove-launcher"]) == 0
    assert not launcher.desktop_file().exists() and not icon.exists()
    assert cli.main(["--remove-launcher"]) == 0
    assert "was not in your app menu" in capsys.readouterr().out


def test_launcher_only_on_linux(monkeypatch, capsys):
    import sys
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cli.main(["--install-launcher"]) == 1
    assert "only available on Linux" in capsys.readouterr().err


def test_version_is_the_same_everywhere():
    import re
    from pathlib import Path

    from gitlane import __version__
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():  # running against an installed copy
        return
    m = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.MULTILINE)
    assert m and m[1] == __version__


# --- stopping ----------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="sends SIGINT, a POSIX signal")
def test_ctrl_c_stops_quietly(make_repo):
    """Ctrl+C must end gitlane with a short message, never a Python traceback."""
    import signal
    import subprocess
    import sys
    import time

    port = cli.find_free_port()
    proc = subprocess.Popen([sys.executable, "-m", "gitlane", str(make_repo()), "--no-browser",
                             "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        deadline = time.time() + 15
        line = ""
        while "Press Ctrl+C" not in line and time.time() < deadline:
            line = proc.stdout.readline()
        assert "Press Ctrl+C" in line
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 0
    assert "Traceback" not in err and "KeyboardInterrupt" not in err
    assert "gitlane stopped." in out


def test_ctrl_c_during_startup_is_quiet(monkeypatch, capsys):
    def boom(argv=None):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "main", boom)
    assert cli.run() == 130
    assert "gitlane stopped." in capsys.readouterr().err
