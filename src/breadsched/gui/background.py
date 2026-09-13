"""Small GTK-safe background-job primitive.

Workers never touch widgets.  Progress and completion are marshalled onto the
default GLib main context, while cancellation is a cooperative threading event.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, TypeVar

from ..gen.utils.cancellation import OperationCancelled
from .gi_setup import GLib

__all__ = ["BackgroundJob", "OperationCancelled"]

T = TypeVar("T")
P = TypeVar("P")


class BackgroundJob(Generic[T, P]):
    """Run one callable off-thread and deliver all callbacks through GLib."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and not self._finished.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()

    def start(
        self,
        work: Callable[[threading.Event, Callable[[P], None]], T],
        on_progress: Callable[[P], None],
        on_success: Callable[[T], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        if self.active:
            raise RuntimeError("background job is already running")

        def deliver(callback, value) -> bool:
            callback(value)
            return GLib.SOURCE_REMOVE

        def finish(callback, value) -> bool:
            try:
                callback(value)
            finally:
                self._finished.set()
            return GLib.SOURCE_REMOVE

        def report(value: P) -> None:
            if self.cancel_event.is_set():
                raise OperationCancelled()
            GLib.idle_add(deliver, on_progress, value)

        def run() -> None:
            try:
                value = work(self.cancel_event, report)
                if self.cancel_event.is_set():
                    raise OperationCancelled()
            except Exception as exc:  # noqa: BLE001 - delivered to the UI
                GLib.idle_add(finish, on_error, exc)
            else:
                GLib.idle_add(finish, on_success, value)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def wait(self, timeout: float = 10.0) -> bool:
        """Wait while pumping GLib; intended for deterministic GUI tests."""
        context = GLib.MainContext.default()
        deadline = GLib.get_monotonic_time() + int(timeout * 1_000_000)
        while not self._finished.is_set() and GLib.get_monotonic_time() < deadline:
            while context.pending():
                context.iteration(False)
            self._finished.wait(0.005)
        while context.pending():
            context.iteration(False)
        return self._finished.is_set()
