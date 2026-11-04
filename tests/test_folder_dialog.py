from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from gitlane import folder_dialog as fd


@pytest.fixture
def tools(monkeypatch):
    """Pretend only the named programs are installed."""
    def _set(*names, platform="linux", display=True):
        monkeypatch.setattr(fd.sys, "platform", platform)
        monkeypatch.setattr(fd.shutil, "which", lambda n: f"/usr/bin/{n}" if n in names else None)
        if display:
            monkeypatch.setenv("DISPLAY", ":0")
        else:
            monkeypatch.delenv("DISPLAY", raising=False)
            monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    return _set


def test_linux_prefers_zenity(tools, tmp_path):
    tools("zenity", "kdialog")
    args, env = fd.command(tmp_path)
    assert args[0] == "/usr/bin/zenity" and "--directory" in args and env == {}
    assert args[-1].startswith("--filename=") and args[-1].endswith(("/", "\\"))  # opens *inside* it


def test_linux_kdialog(tools, tmp_path):
    tools("kdialog")
    args, _ = fd.command(tmp_path)
    assert args == ["/usr/bin/kdialog", "--title", fd.TITLE, "--getexistingdirectory", str(tmp_path)]


def test_linux_without_screen_or_tools(tools, tmp_path):
    tools("zenity", display=False)
    assert fd.command(tmp_path) is None and not fd.available()
    tools()
    assert fd.command(tmp_path) is None


def test_macos_passes_start_as_argument(tools, tmp_path):
    tools("osascript", platform="darwin", display=False)
    tricky = tmp_path / 'say "hi"'
    args, _ = fd.command(tricky)
    assert args[0] == "/usr/bin/osascript" and args[-1] == str(tricky)
    assert str(tricky) not in " ".join(args[:-1])  # never inside the script


def test_windows_passes_start_in_environment(tools, tmp_path):
    tools("powershell", platform="win32", display=False)
    tricky = tmp_path / "it's"
    args, env = fd.command(tricky)
    assert args[0] == "/usr/bin/powershell" and "-STA" in args
    assert env == {"GITLANE_START": str(tricky)} and str(tricky) not in " ".join(args)


def fake_run(monkeypatch, code=0, out="", calls=None):
    def run(args, **kw):
        if calls is not None:
            calls.append((args, kw))
        return subprocess.CompletedProcess(args, code, out, "")
    monkeypatch.setattr(fd.subprocess, "run", run)


def test_choose_returns_folder(tools, monkeypatch, tmp_path):
    tools("zenity")
    calls = []
    fake_run(monkeypatch, out=f"{tmp_path}\n", calls=calls)
    assert fd.choose_folder(tmp_path) == tmp_path
    assert calls[0][1]["stdin"] == subprocess.DEVNULL and "shell" not in calls[0][1]


def test_cancel_returns_none(tools, monkeypatch, tmp_path):
    tools("zenity")
    fake_run(monkeypatch, code=1)
    assert fd.choose_folder(tmp_path) is None
    fake_run(monkeypatch, code=0, out="   \n")
    assert fd.choose_folder(tmp_path) is None


def test_missing_start_folder_uses_home(tools, monkeypatch, tmp_path):
    tools("kdialog")
    calls = []
    fake_run(monkeypatch, code=1, calls=calls)
    fd.choose_folder(tmp_path / "gone")
    assert calls[0][0][-1] == str(Path.home())


def test_no_dialog_available(tools):
    tools(display=False)
    with pytest.raises(fd.DialogError, match="no folder window"):
        fd.choose_folder()


def test_program_fails_to_start(tools, monkeypatch):
    tools("zenity")
    def boom(*a, **k):
        raise OSError("broken")
    monkeypatch.setattr(fd.subprocess, "run", boom)
    with pytest.raises(fd.DialogError, match="Could not open"):
        fd.choose_folder()


def test_only_one_window_at_a_time(tools, monkeypatch, tmp_path):
    tools("zenity")
    entered, release = threading.Event(), threading.Event()
    def slow(args, **kw):
        entered.set()
        release.wait(5)
        return subprocess.CompletedProcess(args, 1, "", "")
    monkeypatch.setattr(fd.subprocess, "run", slow)
    t = threading.Thread(target=fd.choose_folder)
    t.start()
    entered.wait(5)
    try:
        with pytest.raises(fd.DialogBusy, match="already open"):
            fd.choose_folder()
    finally:
        release.set()
        t.join()
    fake_run(monkeypatch, code=1)
    assert fd.choose_folder(tmp_path) is None  # free again
