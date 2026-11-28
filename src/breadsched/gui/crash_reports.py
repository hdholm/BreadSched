"""Record a crash of the desktop, and offer to report it on the next start.

A crash ends the process before anything in it can ask a question: a fault in GTK
or another native library (a segmentation fault, an abort) kills it outright. So:

- at start, :class:`CrashGuard` opens a crash log and has ``faulthandler`` write
  every thread's stack to it if the process dies on a fatal signal; an unhandled
  Python exception (one raised in a GTK callback, which GTK reports and carries on
  from) is written to the same log;
- it also leaves a marker saying the desktop is running, which a clean exit
  removes;
- on the next start, a marker left behind with a log that holds something means
  the last run crashed: the application offers to report it (``pending_crash``),
  the start of the log filled in, as it does for a freeze (``watchdog``).

A process killed outright (``kill -9``, a power cut) writes nothing, so its empty
log is not reported as a crash. A run quit from the not-responding prompt leaves a
hang report instead, which is offered in its place.

The guard runs only in the application the launcher starts, as the watchdog does.
"""

from __future__ import annotations

import faulthandler
import json
import os
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .. import __version__

__all__ = [
    "CrashGuard",
    "CrashReport",
    "MARKER_NAME",
    "clear_crash",
    "crash_to_offer",
    "pending_crash",
]

#: The marker a running desktop leaves; a clean exit removes it.
MARKER_NAME = "running.json"
#: Crash logs kept; older ones are removed.
KEEP_LOGS = 5


@dataclass(frozen=True)
class CrashReport:
    """A run that ended without exiting cleanly, with what its log recorded."""

    when: str
    log: Path


def pending_crash(directory: Path) -> CrashReport | None:
    """The crash the last run left, if it ended without a clean exit and its log
    recorded something (a fatal signal's stacks or an unhandled exception)."""
    try:
        data = json.loads((directory / MARKER_NAME).read_text(encoding="utf-8"))
        log = Path(data["log"])
        started = str(data["started"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        if log.stat().st_size == 0:
            return None
    except OSError:
        return None
    return CrashReport(started, log)


def crash_to_offer(directory: Path, hang_reports: Path | None = None) -> CrashReport | None:
    """The last run's crash to offer on this start, its marker cleared either way;
    None when it closed cleanly, recorded nothing, or was quit as hung (a hang
    report in ``hang_reports`` is offered instead)."""
    from .watchdog import pending_report

    crash = pending_crash(directory)
    clear_crash(directory)
    if crash is not None and hang_reports is not None and pending_report(hang_reports):
        return None
    return crash


def clear_crash(directory: Path) -> None:
    """Forget the last run's marker once its crash has been offered (or the run is
    known to have ended otherwise)."""
    try:
        (directory / MARKER_NAME).unlink()
    except FileNotFoundError:
        pass


class CrashGuard:
    """Write a crash log for this run and mark it running until it exits cleanly."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.log: Path | None = None
        self._handle: TextIO | None = None
        self._previous_hook = sys.excepthook
        self._previous_thread_hook = threading.excepthook

    def start(self) -> CrashGuard:
        """Open this run's crash log, arm ``faulthandler``, and leave the marker.

        Call it after the last run's crash, if any, has been read (``pending_crash``):
        the marker is replaced.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log = self.directory / f"crash-{stamp}-{os.getpid()}.log"
        try:
            self._handle = open(self.log, "w", encoding="utf-8")  # noqa: SIM115 - held open
            faulthandler.enable(file=self._handle, all_threads=True)
        except (OSError, RuntimeError, ValueError):
            self._handle = None
        self._previous_hook = sys.excepthook
        self._previous_thread_hook = threading.excepthook
        sys.excepthook = self._excepthook
        threading.excepthook = self._thread_excepthook
        marker = {
            "started": datetime.now().isoformat(timespec="seconds"),
            "version": __version__,
            "pid": os.getpid(),
            "log": str(self.log),
        }
        try:
            (self.directory / MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
        except OSError:
            pass
        for old in sorted(self.directory.glob("crash-*.log"))[:-KEEP_LOGS]:
            if old != self.log:
                try:
                    old.unlink()
                except OSError:
                    pass
        return self

    def stop(self) -> None:
        """A clean exit: disarm, remove the marker, and drop an empty log."""
        sys.excepthook = self._previous_hook
        threading.excepthook = self._previous_thread_hook
        if self._handle is not None:
            faulthandler.disable()
            self._handle.close()
            self._handle = None
        clear_crash(self.directory)
        if self.log is not None:
            try:
                if self.log.stat().st_size == 0:
                    self.log.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ hooks

    def _write(self, heading: str, lines: list[str]) -> None:
        if self._handle is None:
            return
        at = datetime.now().isoformat(timespec="seconds")
        try:
            self._handle.write(f"BreadSched {__version__}: {heading} at {at}.\n")
            self._handle.writelines(lines)
            self._handle.write("\n")
            self._handle.flush()
        except (OSError, ValueError):
            pass

    def _excepthook(self, kind, value, trace) -> None:
        self._write("an unhandled error", traceback.format_exception(kind, value, trace))
        self._previous_hook(kind, value, trace)

    def _thread_excepthook(self, args) -> None:
        name = args.thread.name if args.thread is not None else "a thread"
        self._write(
            f"an unhandled error in {name}",
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback),
        )
        self._previous_thread_hook(args)
