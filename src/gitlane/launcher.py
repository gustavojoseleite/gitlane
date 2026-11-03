"""App-menu launcher for Linux desktops (``gitlane --install-launcher``).

Writes a freedesktop ``.desktop`` file plus the icon under the user's data
folder (``~/.local/share``). The launcher opens gitlane in a terminal
window, so you can see the link and stop it with Ctrl+C.
"""

from __future__ import annotations

import os
import shlex
import sys
from importlib import resources
from pathlib import Path


class LauncherError(Exception):
    pass


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def desktop_file() -> Path:
    return data_dir() / "applications" / "gitlane.desktop"


def icon_file() -> Path:
    return data_dir() / "icons" / "hicolor" / "scalable" / "apps" / "gitlane.svg"


def _check_platform() -> None:
    if not sys.platform.startswith("linux"):
        raise LauncherError("The app-menu launcher is only available on Linux for now. "
                            "Run `gitlane` from a terminal instead.")


def install() -> str:
    _check_platform()
    icon = icon_file()
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes((resources.files("gitlane") / "static" / "gitlane.svg").read_bytes())
    # The Python that runs gitlane right now: works even when the desktop's PATH
    # doesn't include ~/.local/bin or a virtual environment.
    exec_line = f"{shlex.quote(sys.executable)} -m gitlane"
    entry = desktop_file()
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=gitlane\n"
        "GenericName=Git client\n"
        "Comment=Open your Git repositories in the browser\n"
        f"Exec={exec_line}\n"
        f"Icon={icon}\n"
        "Terminal=true\n"
        "Categories=Development;RevisionControl;\n"
        "Keywords=git;commit;branch;\n",
        encoding="utf-8",
    )
    return (f"Added gitlane to your app menu ({entry}).\n"
            "It opens a terminal window with gitlane; close it or press Ctrl+C to stop.")


def remove() -> str:
    _check_platform()
    removed = [p for p in (desktop_file(), icon_file()) if p.exists()]
    for p in removed:
        p.unlink()
    return "Removed gitlane from your app menu." if removed else "gitlane was not in your app menu."
