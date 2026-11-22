"""Notice when the desktop stops responding, say where it is stuck, and offer a way out.

GTK draws and handles input on one thread. When that thread is stuck (a loop that
never ends, a call that never returns), no window answers, not even the window
manager's close button, and nothing inside the process can ask anything. So:

- the main loop ticks a heartbeat every second (a GLib timeout);
- a watcher thread notices when the ticks stop for ``STALL_SECONDS``, writes every
  thread's stack to a hang log (``faulthandler``), and starts a small separate
  process (``stall_prompt``) that asks whether to wait or quit; the question goes
  away by itself if the ticks resume;
- **Quit** records a hang report and ends this process at once. Each save is a
  complete SQLite transaction, and a book's lock left by a process that is gone is
  replaced as stale, so the book keeps everything saved before the freeze;
- on the next start the application offers to report the problem, the log's
  stacks filled in (``pending_report``).

On POSIX, ``kill -USR1 <pid>`` also writes every thread's stack, on demand, to
``stacks-on-request.log`` in the same directory.

The watchdog runs only in the application the launcher starts: tests drive GTK
without running its main loop between steps, which would look like a freeze.
"""

from __future__ import annotations

import faulthandler
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .. import __version__

__all__ = [
    "EXIT_HUNG",
    "HangReport",
    "STALL_SECONDS",
    "Watchdog",
    "clear_report",
    "log_excerpt",
    "pending_report",
]

#: How long the main loop may go without a tick before the watchdog asks. Opening
#: or upgrading a large book blocks it for seconds by design, so this is well past
#: that, and the question offers to wait.
STALL_SECONDS = 20.0
#: The exit status of a process the user quit after it stopped responding.
EXIT_HUNG = 70
#: Hang logs kept; older ones are removed.
KEEP_LOGS = 5
#: The name of the hang report the next start offers to send.
REPORT_NAME = "last-hang.json"
#: The prompt process's exit status for Quit (anything else is Wait).
PROMPT_QUIT = 3


@dataclass(frozen=True)
class HangReport:
    """A freeze the user quit out of, for the next start to offer to report."""

    when: str
    seconds: float
    log: Path


def pending_report(directory: Path) -> HangReport | None:
    """The hang report left by a process quit after it stopped responding, if any."""
    try:
        data = json.loads((directory / REPORT_NAME).read_text(encoding="utf-8"))
        return HangReport(str(data["when"]), float(data["seconds"]), Path(data["log"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear_report(directory: Path) -> None:
    """Forget the hang report once it has been offered (sent or not)."""
    try:
        (directory / REPORT_NAME).unlink()
    except FileNotFoundError:
        pass


def log_excerpt(log: Path, limit: int = 3000) -> str:
    """The start of a hang log for a bug report, the home directory shown as ``~``.

    The log holds the program's own stack (functions and source lines), never what
    a book contains; the home directory is masked because it names the user.
    """
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    home = str(Path.home())
    if home and home != os.sep:
        text = text.replace(home, "~")
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n… (the rest is in the log file)"
    return text


def _ask_in_another_process(seconds: float, log: Path, resumed: Callable[[], bool]) -> bool:
    """Ask in a separate process whether to quit; False (wait) if it cannot ask.

    The question is withdrawn as soon as the main loop ticks again.
    """
    command = [
        sys.executable,
        "-m",
        "breadsched.gui.stall_prompt",
        f"{seconds:.0f}",
        str(log),
    ]
    try:
        prompt = subprocess.Popen(command)  # noqa: S603 - our own module, fixed arguments
    except OSError:
        return False
    while prompt.poll() is None:
        if resumed():
            prompt.terminate()
            prompt.wait()
            return False
        time.sleep(0.2)
    return prompt.returncode == PROMPT_QUIT


class Watchdog:
    """Watch the GTK main loop's heartbeat from a thread of its own."""

    def __init__(
        self,
        directory: Path,
        *,
        stall_seconds: float = STALL_SECONDS,
        check_every: float = 1.0,
        ask: Callable[[float, Path, Callable[[], bool]], bool] = _ask_in_another_process,
        exit: Callable[[int], object] = os._exit,  # noqa: A002 - mirrors os._exit
    ) -> None:
        self.directory = Path(directory)
        self.stall_seconds = stall_seconds
        self.check_every = check_every
        self._ask = ask
        self._exit = exit
        self._last_beat = time.monotonic()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._beat_source = 0
        self._signal_log: TextIO | None = None
        #: Hang logs written by this watchdog, newest last.
        self.logs: list[Path] = []

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> Watchdog:
        """Start ticking on GTK's loop and watching from a thread."""
        from .gi_setup import GLib

        self.directory.mkdir(parents=True, exist_ok=True)
        self._last_beat = time.monotonic()
        self._beat_source = GLib.timeout_add(1000, self.beat)
        self._thread = threading.Thread(target=self._watch, name="breadsched-watchdog", daemon=True)
        self._thread.start()
        # Windows has neither SIGUSR1 nor faulthandler.register.
        if sys.platform != "win32":
            try:
                log = open(  # noqa: SIM115 - held open for the signal
                    self.directory / "stacks-on-request.log", "a", encoding="utf-8"
                )
                faulthandler.register(signal.SIGUSR1, file=log, all_threads=True)
                self._signal_log = log
            except (OSError, RuntimeError, ValueError):
                self._signal_log = None
        return self

    def stop(self) -> None:
        self._stopped.set()
        if self._beat_source:
            from .gi_setup import GLib

            GLib.source_remove(self._beat_source)
            self._beat_source = 0
        if sys.platform != "win32" and self._signal_log is not None:
            faulthandler.unregister(signal.SIGUSR1)
            self._signal_log.close()
            self._signal_log = None

    def beat(self) -> bool:
        """One tick of the main loop (a GLib timeout callback; keeps itself)."""
        self._last_beat = time.monotonic()
        return True

    # ----------------------------------------------------------------- watching

    def stalled_for(self) -> float:
        return time.monotonic() - self._last_beat

    def _watch(self) -> None:
        while not self._stopped.wait(self.check_every):
            stalled = self.stalled_for()
            if stalled < self.stall_seconds:
                continue
            beat = self._last_beat
            log = self.write_log(stalled)

            def resumed(beat: float = beat) -> bool:
                return self._last_beat != beat

            if self._ask(stalled, log, resumed):
                self._record(stalled, log)
                self._exit(EXIT_HUNG)
                return
            # Waited: ask again only after the loop has ticked and stalled anew.
            while not self._stopped.is_set() and self._last_beat == beat:
                self._stopped.wait(self.check_every)

    def write_log(self, stalled: float) -> Path:
        """Write every thread's stack to a new hang log, keeping the latest few."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log = self.directory / f"hang-{stamp}.log"
        with open(log, "w", encoding="utf-8") as handle:
            handle.write(
                f"BreadSched {__version__} stopped responding: the main loop had not "
                f"run for {stalled:.0f} seconds at {datetime.now().isoformat(timespec='seconds')}."
                f"\nThe main thread, which draws every window, is Thread "
                f"0x{threading.main_thread().ident or 0:016x}; each thread's most recent "
                "call is at the top.\n\n"
            )
            handle.flush()
            faulthandler.dump_traceback(file=handle, all_threads=True)
        self.logs.append(log)
        for old in sorted(self.directory.glob("hang-*.log"))[:-KEEP_LOGS]:
            try:
                old.unlink()
            except OSError:
                pass
        return log

    def _record(self, stalled: float, log: Path) -> None:
        report = {
            "when": datetime.now().isoformat(timespec="seconds"),
            "seconds": round(stalled, 1),
            "log": str(log),
        }
        (self.directory / REPORT_NAME).write_text(json.dumps(report), encoding="utf-8")
