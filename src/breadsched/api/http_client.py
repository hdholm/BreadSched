"""A book API client over HTTP (#357).

**Experimental.** :class:`HttpBookClient` implements
:class:`~breadsched.api.protocol.BookApi` against a running BreadSched web server's
``/api/v1`` resources, so a caller written for the in-process server works
unchanged against a remote one. Results are decoded back into the services' typed
dataclasses, and an error response is raised as the same
:class:`~breadsched.api.contract.ApiError` the in-process server raises.

Every response names the book's revision; the client remembers the last one it
saw (:attr:`HttpBookClient.seen_revision`). With :attr:`require_revision` set, a
write is refused (``412``, ``book.revision.stale``) if the book has moved on.
:meth:`HttpBookClient.subscribe` follows ``/api/v1/events`` on a background thread.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from ..gen.lib import ScheduledTransaction
from ..gen.services import (
    AdvanceDays,
    DueResolution,
    DueScheduleReview,
    NextOccurrence,
    ResolveDue,
    ResolveNext,
    SetAdvanceDays,
)
from .contract import ApiError, to_json
from .protocol import Change, Listener
from .views import (
    advance_days_from_json,
    change_from_json,
    due_review_from_json,
    next_occurrence_from_json,
    resolution_from_json,
)

__all__ = ["HttpBookClient"]


class HttpBookClient:
    """The book API of a BreadSched server at ``base_url``."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("a book server URL is http://host:port")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.token = token
        self.timeout = timeout
        #: The revision named by the last response.
        self.seen_revision: int | None = None
        #: When set, writes send ``If-Match`` with this revision.
        self.require_revision: int | None = None

    # -------------------------------------------------------------- transport

    def _connection(self, timeout: float | None) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _call(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        target = "/api/v1" + path
        present = {key: to_json(value) for key, value in (query or {}).items() if value is not None}
        if present:
            target += "?" + urlencode(present)
        headers = {"X-BreadSched-Token": self.token, "Accept": "application/json"}
        payload = None
        if method != "GET":
            payload = json.dumps(to_json(body or {})).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if self.require_revision is not None:
                headers["If-Match"] = f'"{self.require_revision}"'
        connection = self._connection(self.timeout)
        try:
            connection.request(method, target, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            etag = response.getheader("ETag")
            status = response.status
        finally:
            connection.close()
        if etag:
            self.seen_revision = int(etag.strip('"'))
        data = json.loads(raw or b"null")
        if status != 200:
            data = data if isinstance(data, dict) else {}
            raise ApiError(
                status,
                str(data.get("code", "request.failed")),
                tuple(data.get("fields", ())),
                data.get("error"),
            )
        return data

    # ---------------------------------------------------------------- changes

    @property
    def revision(self) -> int:
        return int(self._call("GET", "/book")["revision"])

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Follow the server's change stream until the returned function is called.

        The listener runs on the stream's thread. If the stream reports changes
        missed since :attr:`seen_revision`, the listener first receives a change
        with no kinds, meaning "reload".
        """
        since = self.seen_revision if self.seen_revision is not None else self.revision
        connection = self._connection(None)
        connection.request(
            "GET",
            "/api/v1/events?" + urlencode({"since": since}),
            headers={"X-BreadSched-Token": self.token, "Accept": "text/event-stream"},
        )
        response = connection.getresponse()
        if response.status != 200:
            connection.close()
            raise ApiError(response.status, "events.unavailable")
        stopped = threading.Event()

        def follow() -> None:
            name = data = ""
            try:
                while not stopped.is_set():
                    line = response.readline()
                    if not line:
                        return
                    text = line.decode("utf-8").rstrip("\r\n")
                    if text.startswith("event:"):
                        name = text[6:].strip()
                    elif text.startswith("data:"):
                        data = text[5:].strip()
                    elif not text and data:
                        payload = json.loads(data)
                        if name == "hello" and payload.get("missed"):
                            listener(Change(int(payload["revision"]), frozenset(), None))
                        elif name == "change":
                            change = change_from_json(payload)
                            self.seen_revision = change.revision
                            listener(change)
                        name = data = ""
            except (OSError, ValueError):
                return

        thread = threading.Thread(target=follow, name="breadsched-events", daemon=True)
        thread.start()

        def stop() -> None:
            stopped.set()
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
            thread.join(timeout=5)

        return stop

    def undo(self) -> bool:
        return bool(self._call("POST", "/undo")["done"])

    def redo(self) -> bool:
        return bool(self._call("POST", "/redo")["done"])

    # ------------------------------------------------- scheduled transactions

    def find_schedule(self, reference: str) -> ScheduledTransaction:
        found = self._call("GET", "/schedules/lookup", {"reference": reference})
        return ScheduledTransaction.from_dict(found["schedule"])

    def due_review(self, as_of: date | None = None) -> list[DueScheduleReview]:
        return due_review_from_json(self._call("GET", "/due-review", {"as_of": as_of}))

    def resolve_due(self, request: ResolveDue) -> DueResolution:
        body = {
            "decisions": [
                {"schedule": handle, "date": when, "decision": decision}
                for handle, when, decision in request.decisions
            ],
            "as_of": request.as_of,
        }
        return resolution_from_json(self._call("POST", "/due-review", body=body))

    def next_occurrence(self, schedule: str, as_of: date | None = None) -> NextOccurrence | None:
        found = self._call("GET", f"/schedules/{quote(schedule, safe='')}/next", {"as_of": as_of})
        return next_occurrence_from_json(found["next"])

    def resolve_next(self, request: ResolveNext) -> DueResolution:
        body = {"decision": request.decision, "date": request.when, "as_of": request.as_of}
        path = f"/schedules/{quote(request.schedule, safe='')}/next"
        return resolution_from_json(self._call("POST", path, body=body))

    def advance_days(self, schedule: str) -> AdvanceDays:
        path = f"/schedules/{quote(schedule, safe='')}/advance-days"
        return advance_days_from_json(self._call("GET", path))

    def set_advance_days(self, request: SetAdvanceDays) -> AdvanceDays:
        body = {"advance_days": request.advance_days, "remind_days": request.remind_days}
        path = f"/schedules/{quote(request.schedule, safe='')}/advance-days"
        return advance_days_from_json(self._call("PATCH", path, body=body))
