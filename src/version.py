"""
version.py

Single source of truth for the app's version string and build date,
shown in the About dialog.

APP_VERSION is bumped manually before each distributed build.
The build date is NOT hardcoded here -- it's read automatically at
runtime from the compiled exe's own file modification date, so it
can never go stale or be forgotten during a build.
"""

import sys
from datetime import datetime
from pathlib import Path

from utils import is_frozen

APP_NAME = "nrtz-desktopApp"
APP_VERSION = "0.2.0"


def get_build_date() -> str:
    """
    Returns the compile/build date as dd.mm.yyyy.

    In a compiled build, this is the modification time of the exe
    itself (i.e. when Nuitka produced it) -- accurate automatically,
    no manual update needed per release.

    In dev mode (running from source), there's no single meaningful
    "build date", so this instead returns this file's own last-edited
    date as a rough stand-in.
    """
    try:
        target = Path(sys.executable) if is_frozen() else Path(__file__)
        ts = target.stat().st_mtime
        return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
    except Exception:
        return "неизвестно"