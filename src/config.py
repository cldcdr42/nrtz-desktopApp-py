"""
User-editable settings, stored at project_root/config/settings.ini.

Defaults are hardcoded here and always used as a fallback — a missing,
deleted, or hand-edited-into-brokenness config file must never crash
the app. If the file doesn't exist yet, it's created with defaults on
first run so there's always something for the user to open and edit.
"""

import configparser
from pathlib import Path
from utils import project_root

_CONFIG_FILENAME = "settings.ini"

DEFAULTS = {
    "mcu": {
        "port": "COM6",
        "baud": "115200",
    },
    "lsl": {
        "data_type": "Data",
        "raw_data_type": "Raw_Data",
        "events_type": "Events",
        # heuristic used to tell the two "Events" streams apart, since
        # LSL 'type' is identical for both — see lsl_stream_worker.py
        "raw_events_name_contains": "raw",
    },
    "plot": {
        "refresh_ms": "50",
        "window_seconds": "10",
    },
}


def config_dir() -> Path:
    d = project_root() / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_defaults(path: Path):
    cp = configparser.ConfigParser()
    cp.read_dict(DEFAULTS)
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)


def load_config() -> configparser.ConfigParser:
    """
    Always returns a usable config: defaults are loaded first, then
    overlaid with whatever's in settings.ini (if it parses cleanly).
    Never raises — a broken file just falls back to defaults, with a
    printed warning so it's visible in the log.
    """
    path = config_dir() / _CONFIG_FILENAME

    cp = configparser.ConfigParser()
    cp.read_dict(DEFAULTS)

    if not path.exists():
        _write_defaults(path)
        print(f"[CONFIG] no settings file found, created defaults at {path}")
        return cp

    try:
        cp.read(path, encoding="utf-8")
    except Exception as e:
        print(f"[CONFIG] failed to parse {path}, using defaults instead: {e}")

    return cp