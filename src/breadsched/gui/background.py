"""Small GTK-safe background-job primitives.

Workers never touch widgets.  Progress and completion are marshalled onto the
default GLib main context, while cancellation is a cooperative threading event.

:class:`SnapshotReader` is the views' shared reader (#389): one worker thread answers
book-server reads from a read-only snapshot of the book, so a long report is
calculated while the window stays usable and only its drawing runs on GTK's loop.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any, Generic, TypeVar

from ..api.local import LocalBookServer
from ..api.protocol import BookApi
from ..gen.db.sqlite import DbSQLite
from ..gen.utils.cancellation import OperationCancelled
from ..gen.utils.logs import get_logger
from .gi_setup import GLib
from .remote_book import OpenBook

__all__ = ["BackgroundJob", "OperationCancelled", "SnapshotReader"]

LOG = get_logger(__name__)

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


#: What a view hands the reader: the read, and what to do with its answer (or failure).
Read = Callable[[BookApi], Any]
Answer = Callable[[Any, BaseException | None], None]


class SnapshotReader:
    """Answer views' book reads from read-only snapshots, on one worker thread.

    A snapshot (``DbSQLite.load(path, "r")``) is an in-memory copy of the committed
    book: a long read neither holds up a save nor sees part of one. One snapshot
    serves every read until the book's revision moves on, so refreshing several
    tabs after a change copies the book once. Reads run one at a time, in the order
    asked; each answer is delivered on GTK's main loop.

    For a book another process holds, ``api`` is that process's book server, which
    answers each read from its own snapshot; the worker only waits for it.
    """

    def __init__(self, path: str | None = None, *, api: BookApi | None = None) -> None:
        if (path is None) == (api is None):
            raise ValueError("a snapshot reader reads a file or a book server, not both")
        self.path = path
        self._api = api
        self._jobs: queue.SimpleQueue[tuple[int, Read, Answer] | None] = queue.SimpleQueue()
        #: Reads asked for whose answers have not been delivered (main loop only).
        self._pending = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="breadsched-reader", daemon=True)
        self._thread.start()

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def submit(self, revision: int, read: Read, answer: Answer) -> None:
        """Run ``read`` against the book as of ``revision`` (or later); ``answer`` it."""
        if self._closed:
            raise RuntimeError("the snapshot reader is closed")
        self._pending += 1
        self._jobs.put((revision, read, answer))

    def _run(self) -> None:
        snapshot: OpenBook | None = None
        server: LocalBookServer | None = None
        held = -1
        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                revision, read, answer = job
                result: Any = None
                error: BaseException | None = None
                try:
                    if self._api is not None:
                        result = read(self._api)
                    else:
                        if snapshot is None or held != revision:
                            if server is not None:
                                server.close()
                            if snapshot is not None:
                                snapshot.close()
                            snapshot = server = None
                            fresh = DbSQLite()
                            assert self.path is not None
                            fresh.load(self.path, "r")
                            snapshot, server, held = fresh, LocalBookServer(fresh), revision
                        assert server is not None
                        result = read(server)
                except Exception as exc:  # noqa: BLE001 - delivered to the view
                    error = exc
                GLib.idle_add(self._deliver, answer, result, error)
        finally:
            if server is not None:
                server.close()
            if snapshot is not None:
                snapshot.close()

    def _deliver(self, answer: Answer, result: Any, error: BaseException | None) -> bool:
        self._pending -= 1
        try:
            answer(result, error)
        except Exception:  # noqa: BLE001 - one view's failure must not stop the others
            LOG.exception("drawing a background read failed")
        return GLib.SOURCE_REMOVE

    def close(self) -> None:
        """Stop after the reads already asked for; their answers are still delivered."""
        if not self._closed:
            self._closed = True
            self._jobs.put(None)

    def wait(self, timeout: float = 10.0) -> bool:
        """Pump GLib until every answer is delivered; deterministic support for tests."""
        context = GLib.MainContext.default()
        deadline = GLib.get_monotonic_time() + int(timeout * 1_000_000)
        while self._pending and GLib.get_monotonic_time() < deadline:
            while context.pending():
                context.iteration(False)
            if self._pending:
                threading.Event().wait(0.005)
        while context.pending():
            context.iteration(False)
        return not self._pending
