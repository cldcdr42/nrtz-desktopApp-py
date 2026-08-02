"""
utils.py

Single source of truth for app-relative paths, correct both for a normal
`python src/main.py` run and for a Nuitka standalone build placed in src/.

Project layout assumed:
    project/
        src/    <- this file, main.py, and the compiled exe all live here
        data/   <- recordings
        logs/   <- debug logs
        temp/   <- scratch/temp files
"""

import sys
import os
from pathlib import Path

APP_NAME = "YourAppName"  # used only for the OS-data-dir fallback below


def is_frozen() -> bool:
    """
    True when running as a compiled build, False in normal `python
    main.py` dev mode.

    sys.frozen is the PyInstaller-style check and isn't reliably set
    by Nuitka. Nuitka's own documented way to detect this is checking
    for `__compiled__`, which it injects as a builtin name at compile
    time — so check both, in case that ever changes across tooling.
    """
    if getattr(sys, "frozen", False):
        return True
    try:
        __compiled__  # noqa: F821 - injected by Nuitka only when compiled
        return True
    except NameError:
        return False


def app_base_dir() -> Path:
    """Folder the running script/exe lives in (src/)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def project_root() -> Path:
    """
    Root directory for writable folders (data/logs/config/temp).

    - Dev mode: main.py runs from src/, so the real project root
      (with data/, logs/, config/, temp/ as siblings of src/) is one
      level up.
    - Compiled (frozen): Nuitka's --standalone build produces a
      self-contained dist folder (exe + every bundled DLL). There is
      no src/-equivalent sibling relationship once that folder is
      copied to wherever it's actually deployed/run — the dist
      folder itself IS the deployable unit, so writable folders are
      created directly beside the exe instead, wherever that turns
      out to be on the target machine.

    This is the only place that encodes the layout difference — if it
    ever changes again, this is the only line to update.
    """
    if is_frozen():
        return app_base_dir()
    return app_base_dir().parent


def resource_path(relative_path: str) -> Path:
    """Read-only bundled resource, next to the app/exe (inside src/)."""
    return app_base_dir() / relative_path


def data_dir(folder_name: str = "data") -> Path:
    """Writable folder for recordings, at project_root/data."""
    return _writable_dir(project_root() / folder_name, folder_name)


def logs_dir(folder_name: str = "logs") -> Path:
    """Writable folder for debug logs, at project_root/logs."""
    return _writable_dir(project_root() / folder_name, folder_name)


def temp_dir(folder_name: str = "temp") -> Path:
    """Writable scratch folder, at project_root/temp."""
    return _writable_dir(project_root() / folder_name, folder_name)


def _writable_dir(preferred: Path, folder_name: str) -> Path:
    """
    Returns `preferred` if it's actually writable (verified with a real
    write test). Falls back to the OS per-user data directory only if
    it genuinely isn't (e.g. project installed under Program Files
    without admin rights).
    """
    if _can_write(preferred):
        return preferred

    fallback = _os_user_data_dir() / folder_name
    fallback.mkdir(parents=True, exist_ok=True)

    print(f"[PATHS] '{preferred}' is not writable, using fallback: {fallback}")

    return fallback


def _can_write(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _os_user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))

    return base / APP_NAME