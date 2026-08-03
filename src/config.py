"""
config.py

User-editable settings, stored at project_root/config/settings.ini.

Defaults are hardcoded here and always used as a fallback -- a missing,
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
        # LSL 'type' is identical for both -- see lsl_thread.py
        "raw_events_name_contains": "raw",
    },
    "plot": {
        "refresh_ms": "50",
        "window_seconds": "10",
    },
    "load_cell": {
        # Fallback span (raw units mapping to +-1.0), used whenever a
        # per-trial calibration isn't run -- either the operator chose
        # "use default values" in the calibration dialog, or one
        # direction's capture never registered a reading. Since the
        # rig is asymmetric and gets re-mounted per subject, this is
        # deliberately a rough one-size-fits-all fallback, not meant
        # to replace real per-trial calibration.
        "default_span": "500000",
    },
}


def config_dir() -> Path:
    d = project_root() / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path() -> Path:
    """Full path to settings.ini, for GUI actions like 'Open Settings File'."""
    return config_dir() / _CONFIG_FILENAME


def _write_defaults(path: Path):
    cp = configparser.ConfigParser()
    cp.read_dict(DEFAULTS)
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)


def load_config() -> configparser.ConfigParser:
    """
    Always returns a usable config: defaults are loaded first, then
    overlaid with whatever's in settings.ini (if it parses cleanly).
    Never raises -- a broken file just falls back to defaults, with a
    printed warning so it's visible in the log.
    """
    path = settings_path()

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


def save_config(cp: configparser.ConfigParser) -> None:
    """
    Writes the given ConfigParser back to settings.ini, overwriting it.
    Used when the GUI changes a setting (e.g. COM port) and needs to
    persist it for the next launch.
    """
    path = settings_path()
    with open(path, "w", encoding="utf-8") as f:
        cp.write(f)