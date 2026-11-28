"""A crash of the desktop is recorded and offered for reporting on the next start
(``gui.crash_reports``). The offer itself is in ``test_gui``; nothing here needs GTK.
"""

from __future__ import annotations

import faulthandler
import json
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from breadsched.gui.crash_reports import (
    MARKER_NAME,
    CrashGuard,
    crash_to_offer,
    pending_crash,
)


@pytest.fixture
def restore_hooks():
    """The guard replaces the exception hooks; put pytest's back whatever happens."""
    hook, thread_hook = sys.excepthook, threading.excepthook
    enabled = faulthandler.is_enabled()
    yield
    sys.excepthook, threading.excepthook = hook, thread_hook
    if enabled:
        faulthandler.enable()


def test_a_clean_exit_leaves_nothing_to_report(tmp_path, restore_hooks):
    guard = CrashGuard(tmp_path).start()
    assert (tmp_path / MARKER_NAME).exists()
    log = guard.log
    assert log is not None and log.exists()
    guard.stop()
    assert not (tmp_path / MARKER_NAME).exists()
    assert not log.exists()  # an empty log is dropped
    assert pending_crash(tmp_path) is None


def test_an_unhandled_error_is_logged_and_a_run_that_never_exits_is_offered(
    tmp_path, restore_hooks
):
    guard = CrashGuard(tmp_path).start()
    calls = []
    guard._previous_hook = lambda *args: calls.append(args)  # quiet in the test
    try:
        raise RuntimeError("boom in a GTK callback")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    assert calls  # the usual report still happens
    # The process ends without a clean exit (the guard never stops).
    report = pending_crash(tmp_path)
    assert report is not None and report.log == guard.log
    text = report.log.read_text(encoding="utf-8")
    assert "an unhandled error" in text and "boom in a GTK callback" in text
    # The next start offers it once: the marker goes.
    assert crash_to_offer(tmp_path) == report
    assert crash_to_offer(tmp_path) is None
    guard._handle and guard._handle.close()


def test_an_unhandled_error_in_a_thread_is_logged(tmp_path, restore_hooks):
    guard = CrashGuard(tmp_path).start()
    guard._previous_thread_hook = lambda args: None

    def fail() -> None:
        raise ValueError("thread failed")

    worker = threading.Thread(target=fail, name="worker")
    worker.start()
    worker.join()
    text = guard.log.read_text(encoding="utf-8")
    assert "an unhandled error in worker" in text and "thread failed" in text
    guard.stop()
    assert pending_crash(tmp_path) is None  # it exited cleanly after all


def test_a_run_killed_outright_records_nothing_and_is_not_offered(tmp_path, restore_hooks):
    guard = CrashGuard(tmp_path).start()
    # As if killed: no stop, nothing written.
    assert pending_crash(tmp_path) is None
    guard._handle.close()
    faulthandler.disable()


def test_a_run_quit_as_hung_is_offered_as_a_hang_not_a_crash(tmp_path, restore_hooks):
    crashes, hangs = tmp_path / "crash", tmp_path / "hang"
    hangs.mkdir()
    guard = CrashGuard(crashes).start()
    guard._previous_hook = lambda *args: None
    try:
        raise RuntimeError("logged before the freeze")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    (hangs / "last-hang.json").write_text(
        json.dumps({"when": "2026-11-28T10:00:00", "seconds": 25, "log": str(hangs / "h.log")}),
        encoding="utf-8",
    )
    assert crash_to_offer(crashes, hangs) is None
    assert not (crashes / MARKER_NAME).exists()
    guard._handle.close()
    faulthandler.disable()


@pytest.mark.skipif(not hasattr(faulthandler, "_sigsegv"), reason="needs faulthandler._sigsegv")
def test_a_segmentation_fault_writes_every_threads_stack(tmp_path):
    """A fault in a native library ends the process at once; faulthandler writes
    the stacks before it goes, and the next start finds them."""
    script = textwrap.dedent(
        f"""
        import faulthandler
        from pathlib import Path
        from breadsched.gui.crash_reports import CrashGuard

        CrashGuard(Path({str(tmp_path)!r})).start()
        faulthandler._sigsegv()
        """
    )
    finished = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", script], capture_output=True, timeout=60
    )
    assert finished.returncode != 0
    report = pending_crash(tmp_path)
    assert report is not None
    text = report.log.read_text(encoding="utf-8", errors="replace")
    assert "Fatal Python error" in text or "fatal exception" in text.lower()
    assert "Current thread" in text or "Thread 0x" in text
    assert Path(report.log).name.startswith("crash-")
