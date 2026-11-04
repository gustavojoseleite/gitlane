"""The system's own "Choose folder" window.

The browser can't give a page the real path of a folder, but gitlane's
server runs on the same computer, so it can open the system dialog itself:

- Linux / BSD: ``zenity`` (GNOME and most desktops) or ``kdialog`` (KDE)
- macOS: ``osascript`` (built in)
- Windows: PowerShell with the Windows Forms folder dialog (built in)

No shell is used and the start folder is never put inside a script: it is
passed as an argument or an environment variable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

TITLE = "Open a repository"
TIMEOUT = 30 * 60  # nobody keeps a dialog open for half an hour

_lock = threading.Lock()

_MAC_SCRIPT = [
    "on run argv",
    f'POSIX path of (choose folder with prompt "{TITLE}" default location (POSIX file (item 1 of argv)))',
    "end run",
]

_WIN_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms;"
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8;"
    "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
    f"$d.Description = '{TITLE}';"
    "$d.ShowNewFolderButton = $false;"
    "$d.SelectedPath = $env:GITLANE_START;"
    "$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true};"
    "if ($d.ShowDialog($owner) -eq 'OK') { Write-Output $d.SelectedPath }"
)


class DialogError(Exception):
    """The dialog could not be shown (the message is safe to show)."""


class DialogBusy(DialogError):
    pass


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def command(start: Path) -> tuple[list[str], dict[str, str]] | None:
    """The command that shows the dialog here, plus extra environment; None if there is none."""
    if sys.platform == "win32":
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if exe:
            return [exe, "-NoProfile", "-NonInteractive", "-STA", "-Command", _WIN_SCRIPT], \
                   {"GITLANE_START": str(start)}
        return None
    if sys.platform == "darwin":
        exe = shutil.which("osascript")
        if exe:
            return [exe, *[a for line in _MAC_SCRIPT for a in ("-e", line)], str(start)], {}
        return None
    if not _has_display():
        return None
    if exe := shutil.which("zenity"):
        # A trailing separator makes zenity start *inside* the folder.
        return [exe, "--file-selection", "--directory", f"--title={TITLE}",
                f"--filename={os.path.join(str(start), '')}"], {}
    if exe := shutil.which("kdialog"):
        return [exe, "--title", TITLE, "--getexistingdirectory", str(start)], {}
    return None


def available() -> bool:
    return command(Path.home()) is not None


def choose_folder(start: str | Path | None = None) -> Path | None:
    """Show the dialog and wait; the chosen folder, or None if the user cancelled."""
    start_path = Path(start) if start else Path.home()
    if not start_path.is_dir():
        start_path = Path.home()
    found = command(start_path)
    if found is None:
        raise DialogError("This computer has no folder window gitlane can use. "
                          "Pick the folder from the list below or paste its path.")
    args, extra_env = found
    if not _lock.acquire(blocking=False):
        raise DialogBusy("A folder window is already open. Look for it behind your browser.")
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env={**os.environ, **extra_env}, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None
    except OSError as e:
        raise DialogError(f"Could not open the folder window: {e}") from None
    finally:
        _lock.release()
    chosen = result.stdout.strip()
    if result.returncode != 0 or not chosen:
        return None  # cancelled (zenity, kdialog and osascript all exit with 1)
    return Path(chosen)
