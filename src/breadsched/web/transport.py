"""Authenticated loopback HTTP transport for the web resource adapters."""

from __future__ import annotations

import json
import os
import queue
import secrets
import socketserver
import threading
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlencode, urlparse

from ..api.protocol import Change
from ..api.views import change_json
from ..gen.db.sqlite import DbSQLite
from ..gen.lib import Money, Rate
from ..gen.utils.logs import get_logger
from .attachment_resource import MAX_ATTACHMENT_BYTES, attachment_content, attachment_upload
from .context import Api
from .controls import ResourceError
from .resources import (
    GET_ROUTES,
    POST_ROUTES,
    V1_EVENTS,
    V1_PREFIX,
    QueryError,
    QueryParams,
    match_v1,
)
from .upload_resource import import_upload

LOG = get_logger(__name__)
T = TypeVar("T")
STATIC = Path(__file__).parent / "static"
#: The page's classic scripts, in the order ``index.html`` loads them. They share one
#: global scope: ``core.js`` first, one script per area, and ``app.js`` (navigation
#: and start-up) last, once every view's renderer is defined.
SCRIPTS = (
    "core.js",
    "controls.js",
    "accounts.js",
    "entry.js",
    "schedules.js",
    "imports.js",
    "plan.js",
    "scenarios.js",
    "review.js",
    "projection.js",
    "household.js",
    "book.js",
    "dashboard.js",
    "app.js",
)
#: The phone-sized quick entry page (``/quick``) loads only these, apart from the
#: full interface, so it opens fast on a small, slow connection.
QUICK_SCRIPTS = ("core.js", "quick.js")
STATIC_ASSETS = {
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "quick": ("quick.html", "text/html; charset=utf-8"),
    "quick.html": ("quick.html", "text/html; charset=utf-8"),
    "quick.js": ("quick.js", "application/javascript; charset=utf-8"),
    "style.css": ("style.css", "text/css; charset=utf-8"),
    "favicon.png": ("favicon.png", "image/png"),
    # Safari asks for this name for an icon added to the home screen.
    "apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    **{name: (name, "application/javascript; charset=utf-8") for name in SCRIPTS},
}
MAX_JSON_BODY = 64 * 1024
MAX_UPLOAD_BODY = 32 * 1024 * 1024


def _encode(value: object) -> object:
    if isinstance(value, Money):
        return str(value.to_decimal())
    if isinstance(value, Rate):
        return str(value.decimal)
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


class Handler(BaseHTTPRequestHandler):
    """Serve static files and dispatch authenticated JSON resource calls."""

    server_version = "BreadSched"
    api_object: Api
    writer_db: DbSQLite
    lock: threading.Lock
    token: str
    #: Origins besides loopback whose pages may use the API: the public origin of
    #: a TLS proxy in front of the server (``serve(trusted_origins=...)``).
    trusted_origins: frozenset[str] = frozenset()
    #: Runs a callable with exclusive use of the writer book (see ``_exclusive``).
    executor: Callable[[Callable[[], Any]], Any]

    _LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - base class name
        LOG.debug("%s %s", self.address_string(), fmt % args)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header(
            "Content-Security-Policy",
            "; ".join(
                (
                    "default-src 'none'",
                    "script-src 'self'",
                    "style-src 'self'",
                    "connect-src 'self'",
                    "img-src 'self' data:",
                    "font-src 'self'",
                    "base-uri 'none'",
                    "form-action 'self'",
                    "frame-ancestors 'none'",
                    "object-src 'none'",
                )
            ),
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: object, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, default=_encode).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(
        self,
        status: int,
        code: str,
        fields: tuple[str, ...] = (),
        *,
        correlation_id: str | None = None,
        message: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "error": message or code,
            "code": code,
            "fields": list(fields),
        }
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id
        self._json(status, payload)

    def _static(self, name: str) -> None:
        asset = STATIC_ASSETS.get(name)
        if asset is None:
            self._error(404, "resource.not_found")
            return
        filename, kind = asset
        root = STATIC.resolve()
        path = (root / filename).resolve()
        if root not in path.parents or not path.is_file():
            self._error(404, "resource.not_found")
            return
        self._send(200, path.read_bytes(), kind)

    def _trusted_host(self) -> bool:
        host = urlparse(f"//{self.headers.get('Host', '')}").hostname
        return host in self._LOCAL_HOSTS

    def _trusted_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        if origin in self.trusted_origins:
            return True
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.hostname in self._LOCAL_HOSTS

    def _trusted_api_request(self, *, write: bool = False) -> bool:
        if not self._trusted_host() or not self._trusted_origin():
            return False
        if write and self.headers.get_content_type() != "application/json":
            return False
        supplied = self.headers.get("X-BreadSched-Token", "")
        return secrets.compare_digest(supplied, self.token)

    def _unexpected(self) -> None:
        correlation_id = secrets.token_hex(8)
        try:
            LOG.exception("request %s failed: %s", correlation_id, self.path)
        except Exception:  # noqa: BLE001 - broken logging must not block the HTTP response
            pass
        self._error(500, "internal.error", correlation_id=correlation_id)

    def _exclusive(self, work: Callable[[], T]) -> T:
        """Run ``work`` with the writer book to itself.

        By default that is under the server's lock, in this request's thread. A host
        that owns the book on another thread (GTK, #359) supplies an executor that
        runs it there instead.
        """
        return self.executor(work)

    def _open_read_api(self) -> tuple[Api, DbSQLite | None]:
        """Open one isolated GET view, or lock the non-reopenable writer."""
        path = self.writer_db.path
        if path is None or path == ":memory:":
            self.lock.acquire()
            return self.api_object, None

        reader = DbSQLite()
        try:
            reader.load(path, mode="r")
        except Exception:
            reader.close()
            raise
        return Api(reader), reader

    def _close_read_api(self, reader: DbSQLite | None) -> None:
        if reader is None:
            self.lock.release()
        else:
            reader.close()

    def do_GET(self) -> None:  # noqa: N802 - required by the base class
        if not self._trusted_host():
            self._error(403, "request.host.untrusted", ("Host",))
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/attachment/content":
            self._attachment_content(parsed.query)
            return
        if parsed.path == V1_EVENTS:
            self._events(parsed.query)
            return
        if parsed.path.startswith(V1_PREFIX + "/"):
            self._v1("GET")
            return
        route = GET_ROUTES.get(parsed.path)
        if route is None:
            self._static("index.html" if parsed.path in ("/", "") else parsed.path[1:])
            return
        if not self._trusted_api_request():
            self._error(403, "request.untrusted")
            return
        try:
            query = QueryParams(parsed.query)
            api, reader = self._open_read_api()
            try:
                result = route(api, query)
            finally:
                self._close_read_api(reader)
            self._json(200, result)
        except QueryError as exc:
            self._error(400, exc.code, exc.fields)
        except ResourceError as exc:
            self._error(exc.status, exc.code, exc.fields, message=exc.message)
        except KeyError:
            self._error(404, "resource.not_found")
        except ValueError as exc:
            self._error(400, "request.invalid", message=str(exc))
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._unexpected()

    def _content_length(self, maximum: int = MAX_JSON_BODY) -> int | None:
        if self.headers.get("Transfer-Encoding") is not None:
            self._error(400, "request.transfer_encoding.unsupported", ("Transfer-Encoding",))
            return None
        values = self.headers.get_all("Content-Length", [])
        if not values:
            self._error(411, "request.content_length.required", ("Content-Length",))
            return None
        if len(values) != 1:
            self._error(400, "request.content_length.repeated", ("Content-Length",))
            return None
        try:
            length = int(values[0])
        except ValueError:
            self._error(400, "request.content_length.invalid", ("Content-Length",))
            return None
        if length < 0:
            self._error(400, "request.content_length.invalid", ("Content-Length",))
            return None
        if length > maximum:
            self._error(413, "request.body.too_large", ("body",))
            return None
        return length

    def _discard_body(self, maximum: int = MAX_JSON_BODY) -> None:
        """Read and drop a bounded request body before rejecting the request.

        Closing a connection with request data still unread makes the operating
        system reset it, and a Windows client then loses the error response.
        """
        values = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or len(values) != 1:
            return
        try:
            length = int(values[0])
        except ValueError:
            return
        if 0 < length <= maximum:
            self.rfile.read(length)

    def do_POST(self) -> None:  # noqa: N802 - required by the base class
        if urlparse(self.path).path == "/api/import/upload":
            self._import_upload()
            return
        if urlparse(self.path).path == "/api/attachment/upload":
            self._attachment_upload()
            return
        if urlparse(self.path).path.startswith(V1_PREFIX + "/"):
            self._v1("POST")
            return
        if not self._trusted_api_request(write=True):
            self._discard_body()
            self._error(403, "request.untrusted")
            return
        parsed = urlparse(self.path)
        route = POST_ROUTES.get(parsed.path)
        if route is None:
            self._discard_body()
            self._error(404, "resource.not_found")
            return
        length = self._content_length()
        if length is None:
            return
        try:
            body: Any = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, "request.body.json_invalid", ("body",))
            return
        if not isinstance(body, dict):
            self._error(400, "request.body.object_required", ("body",))
            return
        try:
            result = self._exclusive(lambda: route(self.api_object, body))
            self._json(200, result)
        except ResourceError as exc:
            self._error(exc.status, exc.code, exc.fields, message=exc.message)
        except KeyError:
            self._error(400, "request.reference.not_found")
        except ValueError as exc:
            self._error(400, "request.invalid", message=str(exc))
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._unexpected()

    def do_PATCH(self) -> None:  # noqa: N802 - required by the base class
        self._v1("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802 - required by the base class
        self._v1("DELETE")

    # ---------------------------------------------------------------- /api/v1

    def _revision_headers(self, revision: int) -> dict[str, str]:
        return {"ETag": f'"{revision}"', "Cache-Control": "no-store"}

    def _if_match(self) -> int | None:
        """The revision a write's ``If-Match`` requires, if it names one."""
        raw = self.headers.get("If-Match")
        if raw is None:
            return None
        try:
            return int(raw.strip().removeprefix("W/").strip('"'))
        except ValueError:
            raise QueryError("request.if_match.invalid", ("If-Match",)) from None

    def _v1(self, method: str) -> None:
        """Serve one versioned book-server resource (#357)."""
        write = method != "GET"
        # A DELETE may carry no body at all; any body it does carry must be JSON.
        bodiless = method == "DELETE" and not self.headers.get_all("Content-Length")
        if not self._trusted_api_request(write=write and not bodiless):
            if write:
                self._discard_body()
            self._error(403, "request.untrusted")
            return
        parsed = urlparse(self.path)
        try:
            matched = match_v1(method, parsed.path)
        except LookupError:
            if write:
                self._discard_body()
            self._error(405, "request.method.not_allowed")
            return
        if matched is None:
            if write:
                self._discard_body()
            self._error(404, "resource.not_found")
            return
        handler, values = matched
        try:
            if not write:
                query = QueryParams(parsed.query)
                api, reader = self._open_read_api()
                try:
                    result = handler(api, values, query)
                    revision = api.book.revision
                finally:
                    self._close_read_api(reader)
                self._json(200, result, self._revision_headers(revision))
                return
            if parsed.query:
                self._discard_body()
                raise QueryError("query.unknown", ())
            length = 0 if bodiless else self._content_length()
            if length is None:
                return
            try:
                body: Any = json.loads((self.rfile.read(length) if length else b"") or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._error(400, "request.body.json_invalid", ("body",))
                return
            if not isinstance(body, dict):
                self._error(400, "request.body.object_required", ("body",))
                return
            required = self._if_match()

            def perform() -> tuple[object, int]:
                book = self.api_object.book
                if required is not None and required != book.revision:
                    raise ResourceError(
                        412,
                        "book.revision.stale",
                        ("If-Match",),
                        "The book changed since it was read; reload and try again",
                    )
                result = handler(self.api_object, values, body)
                return result, book.revision

            result, revision = self._exclusive(perform)
            self._json(200, result, self._revision_headers(revision))
        except QueryError as exc:
            self._error(400, exc.code, exc.fields)
        except ResourceError as exc:
            self._error(exc.status, exc.code, exc.fields, message=exc.message)
        except KeyError:
            self._error(404 if not write else 400, "request.reference.not_found")
        except ValueError as exc:
            self._error(400, "request.invalid", message=str(exc))
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._unexpected()

    #: Seconds between keep-alive comments on an idle change stream.
    EVENT_KEEPALIVE = 15.0

    def _events(self, raw_query: str) -> None:
        """Stream the book's changes as server-sent events (#357).

        The first event, ``hello``, carries the current revision; a client that
        asked for changes ``since`` an older one has missed some and should reload.
        Each later ``change`` event is one :class:`~breadsched.api.protocol.Change`.
        """
        if not self._trusted_api_request():
            self._error(403, "request.untrusted")
            return
        try:
            query = QueryParams(raw_query)
            since = query.integer("since", minimum=0)
            query.finish()
        except QueryError as exc:
            self._error(400, exc.code, exc.fields)
            return
        changes: queue.Queue[Change] = queue.Queue()

        def follow() -> tuple[Callable[[], None], int]:
            book = self.api_object.book
            return book.subscribe(changes.put), book.revision

        stop, revision = self._exclusive(follow)
        stopping = getattr(self.server, "stopping", None)
        try:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            hello = {"revision": revision, "missed": since is not None and since < revision}
            self._event("hello", revision, hello)
            while stopping is None or not stopping.is_set():
                try:
                    change = changes.get(timeout=self.EVENT_KEEPALIVE)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                self._event("change", change.revision, change_json(change))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            stop()

    def _event(self, name: str, revision: int, payload: object) -> None:
        data = json.dumps(payload, default=_encode)
        self.wfile.write(f"event: {name}\nid: {revision}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def _import_upload(self) -> None:
        """Receive one bounded file without JSON encoding or a client filesystem path."""
        if (
            not self._trusted_api_request()
            or self.headers.get_content_type() != "application/octet-stream"
        ):
            self._discard_body(MAX_UPLOAD_BODY)
            self._error(403, "request.untrusted")
            return
        try:
            query = QueryParams(urlparse(self.path).query)
            filename = query.text("filename", required=True)
            number_format = query.text("number_format") or "auto"
            date_format = query.text("date_format") or "auto"
            duplicates = query.text("include_duplicates") or "0"
            if duplicates not in {"0", "1"}:
                raise QueryError("query.invalid", ("include_duplicates",))
            query.finish()
        except QueryError as exc:
            self._discard_body(MAX_UPLOAD_BODY)
            self._error(400, exc.code, exc.fields)
            return
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._discard_body(MAX_UPLOAD_BODY)
            self._unexpected()
            return
        try:
            assert filename is not None
            length = self._content_length(MAX_UPLOAD_BODY)
            if length is None:
                return
            content = self.rfile.read(length)
            if len(content) != length:
                self._error(400, "request.body.incomplete", ("file",))
                return
            result = self._exclusive(
                lambda: import_upload(
                    self.api_object,
                    self.writer_db,
                    filename=filename,
                    content=content,
                    number_format=number_format,
                    date_format=date_format,
                    include_duplicates=duplicates == "1",
                )
            )
            self._json(200, result)
        except ResourceError as exc:
            self._error(exc.status, exc.code, exc.fields, message=exc.message)
        except ValueError as exc:
            self._error(400, "request.invalid", message=str(exc))
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._unexpected()

    def _attachment_content(self, raw_query: str) -> None:
        """Send one document a transaction lists, as bytes the page opens or saves."""
        if not self._trusted_api_request():
            self._error(403, "request.untrusted")
            return
        try:
            query = QueryParams(raw_query)
            transaction = query.text("transaction", required=True) or ""
            location = query.text("location", required=True) or ""
            query.finish()
            api, reader = self._open_read_api()
            try:
                content, kind = attachment_content(api.db, transaction, location)
            finally:
                self._close_read_api(reader)
            self._send(200, content, kind)
        except QueryError as exc:
            self._error(400, exc.code, exc.fields)
        except ResourceError as exc:
            self._error(exc.status, exc.code, exc.fields, message=exc.message)
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._unexpected()

    def _attachment_upload(self) -> None:
        """Receive one bounded document for a transaction, without a client path."""
        if (
            not self._trusted_api_request()
            or self.headers.get_content_type() != "application/octet-stream"
        ):
            self._discard_body(MAX_ATTACHMENT_BYTES)
            self._error(403, "request.untrusted")
            return
        try:
            query = QueryParams(urlparse(self.path).query)
            transaction = query.text("transaction", required=True) or ""
            filename = query.text("filename", required=True) or ""
            query.finish()
        except QueryError as exc:
            self._discard_body(MAX_ATTACHMENT_BYTES)
            self._error(400, exc.code, exc.fields)
            return
        try:
            length = self._content_length(MAX_ATTACHMENT_BYTES)
            if length is None:
                return
            content = self.rfile.read(length)
            if len(content) != length:
                self._error(400, "request.body.incomplete", ("file",))
                return
            result = self._exclusive(
                lambda: attachment_upload(
                    self.api_object,
                    self.writer_db,
                    transaction=transaction,
                    filename=filename,
                    content=content,
                )
            )
            self._json(200, result)
        except ResourceError as exc:
            self._error(exc.status, exc.code, exc.fields, message=exc.message)
        except ValueError as exc:
            self._error(400, "request.invalid", message=str(exc))
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._unexpected()


class BreadSchedHTTPServer(ThreadingHTTPServer):
    """Threaded local server carrying the API token exposed to the launcher."""

    token: str
    #: Set on shutdown so open change streams end.
    stopping: threading.Event

    def shutdown(self) -> None:
        self.stopping.set()
        super().shutdown()

    def server_bind(self) -> None:
        """Bind without ``HTTPServer``'s reverse lookup of the loopback address.

        ``socket.getfqdn`` can stall for many seconds where reverse DNS is slow
        (macOS runners), delaying every start; the name is only informational.
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


Executor = Callable[[Callable[[], Any]], Any]


def trusted_origin(raw: str) -> str:
    """``raw`` as an origin a proxied page may send, or a ``ValueError``.

    Only ``https://host`` or ``https://host:port`` is accepted: a page reaching the
    book from the network must have come over TLS.
    """
    parsed = urlparse(raw.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError(f"a trusted origin is https://host[:port], not {raw!r}")
    port = f":{parsed.port}" if parsed.port not in (None, 443) else ""
    return f"https://{parsed.hostname}{port}"


#: The shortest token a token file may hold.
MIN_TOKEN_LENGTH = 32


def token_from_file(path: str | Path) -> str:
    """The server token kept in ``path``, creating it (readable only by the user) if absent.

    A kept token survives restarts, so a client given it once keeps working. A file
    others can read, or a token too short to guess at, is refused.
    """
    target = Path(path)
    if not target.exists():
        token = secrets.token_urlsafe(32)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        return token
    if os.name == "posix" and target.stat().st_mode & 0o077:
        raise ValueError(f"{target} can be read by other users; make it private (chmod 600)")
    token = target.read_text(encoding="utf-8").strip()
    if len(token) < MIN_TOKEN_LENGTH or any(char.isspace() for char in token):
        raise ValueError(f"{target} must hold one token of at least {MIN_TOKEN_LENGTH} characters")
    return token


def build_handler(
    db: DbSQLite,
    token: str,
    executor: Executor | None = None,
    trusted_origins: frozenset[str] = frozenset(),
) -> type[Handler]:
    """Bind one serialized writer and short-lived read snapshots to a handler.

    Writer work runs under one lock in the request's thread unless ``executor``
    runs it elsewhere (GTK runs it on its main loop, #359).
    """
    lock = threading.Lock()

    def locked(work: Callable[[], Any]) -> Any:
        with lock:
            return work()

    return type(
        "BoundHandler",
        (Handler,),
        {
            "api_object": Api(db),
            "writer_db": db,
            "lock": lock,
            "token": token,
            "trusted_origins": trusted_origins,
            "executor": staticmethod(executor or locked),
        },
    )


def serve(
    db: DbSQLite,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    executor: Executor | None = None,
    *,
    token: str | None = None,
    trusted_origins: tuple[str, ...] = (),
) -> BreadSchedHTTPServer:
    """Start the server. Returns it without blocking; call ``serve_forever``.

    ``token`` is a kept token (``token_from_file``), else a new one is made.
    ``trusted_origins`` are the public origins of a TLS proxy in front of it.
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the web interface binds to loopback only")
    origins = frozenset(trusted_origin(origin) for origin in trusted_origins)
    token = token or secrets.token_urlsafe(32)
    server = BreadSchedHTTPServer(
        (host, port), build_handler(db, token, executor, trusted_origins=origins)
    )
    server.token = token
    server.stopping = threading.Event()
    if open_browser:  # pragma: no cover - depends on a desktop session
        import webbrowser

        query = urlencode({"token": token})
        threading.Timer(
            0.5,
            partial(webbrowser.open, f"http://{host}:{server.server_port}/#{query}"),
        ).start()
    return server
