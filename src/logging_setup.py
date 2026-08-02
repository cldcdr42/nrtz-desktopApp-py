"""
Centralized logging setup for the whole app.

Call init_logging() exactly once, as early as possible in main.py —
before other project modules run any code that logs — so every
module's log_print()/log_exception() calls land in the same file.

This matters more than it might look: build.bat compiles with
--windows-console-mode=disable, so in the compiled app there is no
console for print() to write to at all. Without this, only whichever
module explicitly duplicates its output to a file (previously just
mcu_thread_debug.py) produces any diagnostic trail once deployed.
"""

import logging
import traceback

from utils import logs_dir

_initialized = False

# Third-party libraries that also use the stdlib logging module and
# are extremely verbose at DEBUG level (matplotlib's font matching in
# particular can produce hundreds of lines per plot). Silencing these
# specifically keeps app.log at DEBUG for OUR code without drowning in
# library internals. Add to this list if another library turns out to
# be similarly noisy.
_NOISY_LOGGERS = [
    "matplotlib",
    "PIL",
    "urllib3",
]


def init_logging(filename: str = "app.log", level=logging.DEBUG) -> None:
    global _initialized

    if _initialized:
        return

    log_file = logs_dir() / filename

    logging.basicConfig(
        filename=str(log_file),
        level=level,
        format="%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s",
    )

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _initialized = True

    logging.info(f"===== Logging started -> {log_file} =====")


def log_print(message: str) -> None:
    """
    Print to console (when one exists — dev mode) AND write to the
    shared log file (always, including the console-disabled build).
    """
    print(message, flush=True)
    logging.info(message)


def log_exception(message: str) -> None:
    """
    Print the traceback to console (dev mode) and write both the
    message and full traceback to the shared log file.
    """
    print(message, flush=True)
    traceback.print_exc()
    logging.exception(message)