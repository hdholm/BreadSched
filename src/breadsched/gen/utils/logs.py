"""Logging setup.

One place decides where log records go, so ``--debug`` behaves identically whether
it came from the command line, the import dialog, or a test. The library code
itself only ever calls ``logging.getLogger(__name__)`` and never configures
anything, which is what lets an embedding application keep control of its own
handlers.
"""

from __future__ import annotations

import logging
from pathlib import Path

__all__ = ["configure", "get_logger", "LEVELS"]

#: Verbosity count -> console level. Repeating ``-v`` walks down this list.
#:
#: The default is ERROR rather than WARNING because the operations that generate
#: warnings -- imports, chiefly -- already collect them and present them in their
#: own report. Letting the logger print them too would show every warning twice.
LEVELS = [logging.ERROR, logging.INFO, logging.DEBUG]

_ROOT = "breadsched"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
#: Marks a handler as installed by configure(), and therefore ours to replace.
_OWNED = "_breadsched_managed"
_TIME = "%H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def configure(
    verbosity: int = 0,
    path: str | Path | None = None,
    stream: bool = True,
) -> Path | None:
    """Set up logging for the ``breadsched`` tree and return the log file, if any.

    Called more than once, it replaces its own handlers rather than stacking them,
    so re-running an import from the GUI does not double every line.
    """
    logger = logging.getLogger(_ROOT)
    level = LEVELS[min(verbosity, len(LEVELS) - 1)]
    logger.setLevel(level)
    # Do not let records escape to the root logger, which an embedding application
    # may have configured for something else entirely.
    logger.propagate = False

    # Remove only the handlers this function installed. A handler attached by an
    # embedding application, or by a test that wants to observe us, is not ours to
    # discard -- and silently discarding it makes for a mystifying afternoon.
    for handler in list(logger.handlers):
        if getattr(handler, _OWNED, False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(_FORMAT, datefmt=_TIME)

    if stream:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(formatter)
        setattr(console, _OWNED, True)
        logger.addHandler(console)

    written: Path | None = None
    if path is not None:
        written = Path(path).expanduser()
        written.parent.mkdir(parents=True, exist_ok=True)
        to_file = logging.FileHandler(written, mode="w", encoding="utf-8")
        # A log file is only ever asked for when something needs diagnosing, so it
        # always gets full detail regardless of the console verbosity.
        to_file.setLevel(logging.DEBUG)
        to_file.setFormatter(formatter)
        setattr(to_file, _OWNED, True)
        logger.addHandler(to_file)
        logger.setLevel(min(level, logging.DEBUG))

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return written
