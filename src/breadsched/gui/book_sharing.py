"""Share the book open in GTK with other BreadSched commands (#359, experimental).

When **Preferences → Sharing → Let other BreadSched commands use the open book**
is on, GTK serves its writable book on the loopback interface, exactly as
``breadsched serve`` does, and publishes a server record (``api.discovery``).
Book-server commands for that book (``due-review``, ``scheduled-next``,
``scheduled-advance``) then work through this window instead of stopping on the
writer lock.

The HTTP server runs on its own thread, but every request that uses the book runs
on the GTK main loop (:func:`main_loop_executor`), the only thread that touches the
book. So storage signals, and with them the views' redraw-on-change, fire there as
they do for GTK's own edits. Reads that open a read-only snapshot stay on the
server's thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..api.discovery import ServerRecord, publish_server, withdraw_server
from ..gen.db.sqlite import DbSQLite
from ..gen.utils.logs import get_logger
from .gi_setup import GLib

__all__ = ["SETTING", "SharedBook", "main_loop_executor", "share_book"]

LOG = get_logger(__name__)

#: The preference (section, key) that turns sharing on.
SETTING = ("general", "share_book")


def main_loop_executor(work: Callable[[], Any]) -> Any:
    """Run ``work`` on the GTK main loop and wait for its result (or exception)."""
    if threading.current_thread() is threading.main_thread():
        return work()
    done = threading.Event()
    outcome: dict[str, Any] = {}

    def call() -> bool:
        try:
            outcome["value"] = work()
        except BaseException as exc:  # noqa: BLE001 - handed back to the request thread
            outcome["error"] = exc
        finally:
            done.set()
        return False

    GLib.idle_add(call)
    done.wait()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


@dataclass(slots=True)
class SharedBook:
    """A running share of one book; :meth:`stop` ends it."""

    path: str
    url: str
    record: ServerRecord
    _httpd: Any
    _thread: threading.Thread

    def stop(self) -> None:
        withdraw_server(self.record)
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def share_book(db: DbSQLite, path: str) -> SharedBook | None:
    """Serve ``db`` (open for writing at ``path``) to other commands; None if not possible."""
    from ..web.transport import serve

    lock = db.writer_lock_token
    if lock is None:  # read-only: nothing to share
        return None
    try:
        httpd = serve(db, host="127.0.0.1", port=0, executor=main_loop_executor)
    except OSError as exc:
        LOG.warning("could not share the book: %s", exc)
        return None
    url = f"http://127.0.0.1:{httpd.server_port}"
    thread = threading.Thread(target=httpd.serve_forever, name="breadsched-share", daemon=True)
    thread.start()
    record = publish_server(path, url, httpd.token, lock)
    return SharedBook(path, url, record, httpd, thread)
