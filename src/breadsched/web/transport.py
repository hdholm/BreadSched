"""Authenticated loopback HTTP transport for the web resource adapters."""

from __future__ import annotations

import getpass
import hmac
import json
import os
import queue
import re
import secrets
import socketserver
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import partial
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import unquote, urlencode, urlparse

from ..api.contract import ApiError
from ..api.host import (
    BOOK_HEADER,
    HostedBook,
    JoinedBooks,
    choose_book,
    host_refusal,
    hosted_name,
)
from ..api.local import local_caller
from ..api.permissions import acting_as
from ..api.protocol import Change
from ..api.typed_codec import to_data
from ..api.users import SESSION_DAYS, Caller, User, UserDirectory, UserError
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
#: The browser's session cookie (decision 0004).
SESSION_COOKIE = "breadsched_session"
ACCOUNT_SESSION = "/api/v1/session"
ACCOUNT_ME = "/api/v1/me"
ACCOUNT_PASSWORD = "/api/v1/me/password"
#: The books this host holds that the caller may use (decision 0004).
ACCOUNT_BOOKS = "/api/v1/books"
#: Answered before the caller is known (signing in) or while a temporary password
#: must still be changed, or for no one book.
ACCOUNT_PATHS = frozenset({ACCOUNT_SESSION, ACCOUNT_ME, ACCOUNT_PASSWORD, ACCOUNT_BOOKS})
#: Another command of the computer account asking the host to serve a book, or to let
#: it go (decision 0004: one host per account).
HOST_BOOKS = "/api/v1/host/books"


@dataclass(frozen=True, slots=True)
class _Book:
    """One book a host serves: its writer, read snapshots, and who may use it."""

    #: What a request names it by: its permanent id, or a stand-in for a book that
    #: has none (read-only or in memory), which no grant can name.
    key: str
    #: The permanent id grants name, if the book has one.
    book_id: str | None
    name: str
    api: Api
    db: DbSQLite
    lock: threading.Lock
    #: Runs a callable with exclusive use of the writer book (see ``_exclusive``).
    executor: Callable[[Callable[[], Any]], Any]


def _me(caller: Caller) -> dict[str, object]:
    user = caller.user
    return {
        "name": user.name,
        "display_name": user.display_name,
        "administrator": user.administrator,
        "must_change": user.must_change,
        "this_computer": caller.host_files,
    }


#: The book server's two routes that take a file's bytes rather than JSON.
_V1_UPLOAD = re.compile(
    r"^/api/v1/(?:imports/upload|transactions/(?P<transaction>[^/]+)/attachments/upload)$"
)
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
    #: The books this host serves, by the key a request names (``X-BreadSched-Book``).
    books: dict[str, _Book]
    token: str
    #: Origins besides loopback whose pages may use the API: the public origin of
    #: a TLS proxy in front of the server (``serve(trusted_origins=...)``).
    trusted_origins: frozenset[str] = frozenset()
    #: The second token that marks a request from a program on this computer.
    local_token: str = ""
    #: The host's users and device tokens (decision 0004).
    users: UserDirectory | None = None

    _LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

    #: Per request: why ``_trusted_api_request`` turned it away, the book it named,
    #: and who sent it. One handler serves every request on a kept-alive connection.
    _why: ApiError | None = None
    _chosen: tuple[str, _Book] | None = None
    _resolved: tuple[str, Caller | None] | None = None

    def parse_request(self) -> bool:
        self._why = None
        self._chosen = None
        self._resolved = None
        return super().parse_request()

    # ------------------------------------------------------------ the book asked for

    def _book(self) -> _Book:
        """The book this request is for: the one its ``X-BreadSched-Book`` names, or
        the only one the host holds. An ``ApiError`` if it names none of them."""
        wanted = self.headers.get(BOOK_HEADER, "").strip()
        cached = self._chosen
        if cached is not None and cached[0] == wanted:
            return cached[1]
        chosen = choose_book(self.books, wanted)
        self._chosen = (wanted, chosen)
        return chosen

    @property
    def api_object(self) -> Api:
        return self._book().api

    @property
    def writer_db(self) -> DbSQLite:
        return self._book().db

    @property
    def lock(self) -> threading.Lock:
        return self._book().lock

    @property
    def executor(self) -> Callable[[Callable[[], Any]], Any]:
        return self._book().executor

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
        self._why = None
        if not self._trusted_host() or not self._trusted_origin():
            return False
        if write and self.headers.get_content_type() != "application/json":
            return False
        caller = self._caller()
        # A temporary password must be changed before anything else.
        if caller is None or caller.user.must_change:
            return False
        try:
            self._book()
        except ApiError as refused:
            # Only someone the host knows learns that the book they named is not here.
            self._why = refused
            return False
        # A user given no role in this book may not use it at all (decision 0004).
        return self._role() != "none"

    def _refuse(self) -> None:
        """Answer a request ``_trusted_api_request`` turned away."""
        refused = self._why
        if refused is None:
            self._error(403, "request.untrusted")
        else:
            self._error(refused.status, refused.code, refused.fields, message=refused.message)

    def _role(self, book: _Book | None = None) -> str:
        """The caller's role in ``book`` (the one asked for); "none" for anyone without
        one, or when the request names no book here."""
        caller = self._caller()
        if caller is None:
            return "none"
        if caller.user.host_account:
            return "owner"
        if self.users is None:
            return "none"
        try:
            book_id = (book or self._book()).book_id
        except ApiError:
            return "none"
        try:
            return self.users.role(book_id, caller.user) or "none"
        except (OSError, sqlite3.Error):
            LOG.exception("could not read the user directory")
            return "none"

    def _supplied_token(self) -> str:
        """The token in ``X-BreadSched-Token``, else the browser's session cookie."""
        header = self.headers.get("X-BreadSched-Token", "")
        if header:
            return header
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel is not None else ""

    def _caller(self) -> Caller | None:
        """Who the request comes from (decision 0004): this server's own token (or its
        local token) is the computer account's user; any other token must be a device
        token or session the host issued. None for anyone else."""
        supplied = self._supplied_token()
        if not supplied:
            return None
        cached = self._resolved
        if cached is not None and cached[0] == supplied:
            return cached[1]
        caller: Caller | None = None
        if secrets.compare_digest(supplied, self.token):
            caller = Caller(self._host_user(), False)
        elif self.local_token and secrets.compare_digest(supplied, self.local_token):
            caller = Caller(self._host_user(), True)
        elif self.users is not None:
            try:
                caller = self.users.authenticate(supplied)
            except (OSError, sqlite3.Error):
                LOG.exception("could not read the user directory")
        self._resolved = (supplied, caller)
        return caller

    def _host_user(self) -> User:
        """The computer account's user, or a stand-in when no directory can be kept."""
        if self.users is not None:
            try:
                return self.users.host_user()
            except (OSError, sqlite3.Error):
                LOG.exception("could not read the user directory")
        name = getpass.getuser()
        return User("", name, name, True, True)

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
        runs it there instead. Either way it runs as from a program on this computer
        only when the request carries the server's local token (#358).
        """
        local = self._local_request()
        role = self._role()

        def as_caller() -> T:
            with local_caller(local), acting_as(role):
                return work()

        return self.executor(as_caller)

    def _local_request(self) -> bool:
        """Whether the request carries the local token: only programs that read the
        server's record (a file only its user can read), or the page the server
        itself opened, hold it; a device reaching the server through a proxy does not."""
        caller = self._caller()
        if caller is not None and caller.host_files:
            return True
        offered = self.headers.get("X-BreadSched-Local", "")
        return bool(self.local_token and offered) and hmac.compare_digest(
            offered.encode("utf-8"), self.local_token.encode("utf-8")
        )

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
        # Reads run in this thread: the user's role guards every operation they reach.
        with acting_as(self._role()):
            self._get()

    def _get(self) -> None:
        if not self._trusted_host():
            self._error(403, "request.host.untrusted", ("Host",))
            return
        parsed = urlparse(self.path)
        if parsed.path in ACCOUNT_PATHS:
            self._account("GET")
            return
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
            self._refuse()
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
        if urlparse(self.path).path == HOST_BOOKS:
            self._host_books("POST")
            return
        if urlparse(self.path).path in ACCOUNT_PATHS:
            self._account("POST")
            return
        if urlparse(self.path).path == "/api/import/upload":
            self._import_upload()
            return
        if urlparse(self.path).path == "/api/attachment/upload":
            self._attachment_upload()
            return
        upload = _V1_UPLOAD.match(urlparse(self.path).path)
        if upload is not None:
            self._v1_upload(upload["transaction"])
            return
        if urlparse(self.path).path.startswith(V1_PREFIX + "/"):
            self._v1("POST")
            return
        if not self._trusted_api_request(write=True):
            self._discard_body()
            self._refuse()
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
        if urlparse(self.path).path.startswith(HOST_BOOKS + "/"):
            self._host_books("DELETE")
            return
        if urlparse(self.path).path in ACCOUNT_PATHS:
            self._account("DELETE")
            return
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

    # ------------------------------------------------------------- signing in

    def _account(self, method: str) -> None:
        """Sign in or out, say who is signed in, or change one's own password
        (decision 0004). These are answered before the caller is known, or while a
        temporary password still has to be changed."""
        path = urlparse(self.path).path
        write = method != "GET"
        if (
            not self._trusted_host()
            or not self._trusted_origin()
            or (method == "POST" and self.headers.get_content_type() != "application/json")
        ):
            if write:
                self._discard_body()
            self._refuse()
            return
        if self.users is None:
            if write:
                self._discard_body()
            self._error(404, "resource.not_found")
            return
        try:
            body: dict[str, Any] = {}
            if method == "POST":
                length = self._content_length()
                if length is None:
                    return
                loaded = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(loaded, dict):
                    self._error(400, "request.body.object_required", ("body",))
                    return
                body = loaded
            if (method, path) == ("POST", ACCOUNT_SESSION):
                self._sign_in(body)
                return
            if (method, path) == ("DELETE", ACCOUNT_SESSION):
                supplied = self._supplied_token()
                ended = bool(supplied) and self.users.sign_out(supplied)
                self._json(200, {"signed_out": ended}, {"Set-Cookie": self._cookie("", 0)})
                return
            caller = self._caller()
            if caller is None:
                self._refuse()
                return
            if (method, path) == ("GET", ACCOUNT_ME):
                self._json(200, _me(caller))
                return
            if (method, path) == ("GET", ACCOUNT_BOOKS):
                if caller.user.must_change:
                    self._error(403, "request.untrusted")
                    return
                self._json(200, {"books": [to_data(book) for book in self._usable_books()]})
                return
            if (method, path) == ("POST", ACCOUNT_PASSWORD):
                changed = self.users.set_password(
                    caller.user.name,
                    str(body.get("new", "")),
                    current=str(body.get("current", "")),
                    keep=self._supplied_token(),
                )
                self._json(200, _me(Caller(changed, caller.host_files)))
                return
            self._error(405, "request.method.not_allowed")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, "request.body.json_invalid", ("body",))
        except UserError as exc:
            status = {"signin.failed": 401, "signin.throttled": 429}.get(exc.code, 400)
            self._error(status, exc.code, exc.fields, message=exc.message)
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._unexpected()

    def _host_books(self, method: str) -> None:
        """Open a book for another of the computer account's commands, or let one go.
        Only the account itself, from this computer, may change what the host serves."""
        joined: JoinedBooks | None = getattr(self.server, "joined", None)
        if (
            not self._trusted_host()
            or not self._trusted_origin()
            or (method == "POST" and self.headers.get_content_type() != "application/json")
        ):
            self._discard_body()
            self._error(403, "request.untrusted")
            return
        caller = self._caller()
        if caller is None:
            self._discard_body()
            self._error(403, "request.untrusted")
            return
        if not caller.user.host_account or not self._local_request():
            self._discard_body()
            refused = host_refusal(403, "host.local_only")
            self._error(refused.status, refused.code, message=refused.message)
            return
        if joined is None:
            self._discard_body()
            self._error(404, "resource.not_found")
            return
        try:
            if method == "POST":
                length = self._content_length()
                if length is None:
                    return
                body = json.loads(self.rfile.read(length) or b"{}")
                path = body.get("path") if isinstance(body, dict) else None
                if not isinstance(path, str) or not path:
                    self._error(400, "request.invalid", ("path",))
                    return
                self._json(200, {"id": joined.open(path)})
                return
            self._discard_body()
            key = unquote(urlparse(self.path).path[len(HOST_BOOKS) + 1 :])
            if not joined.close(key):
                raise host_refusal(404, "host.book.not_joined")
            self._json(200, {"closed": key})
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, "request.body.json_invalid", ("body",))
        except ApiError as exc:
            self._error(exc.status, exc.code, exc.fields, message=exc.message)
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._unexpected()

    def _usable_books(self) -> list[HostedBook]:
        """The books here the caller has a role in, by name."""
        usable = []
        for book in self.books.values():
            role = self._role(book)
            if role != "none":
                usable.append(HostedBook(book.key, book.name, role))
        return sorted(usable, key=lambda book: (book.name.casefold(), book.id))

    def _sign_in(self, body: dict[str, Any]) -> None:
        assert self.users is not None
        # Behind the proxy every request comes from loopback; the proxy names the
        # client in X-Real-IP (decision 0003's nginx configuration).
        client = self.headers.get("X-Real-IP") or self.client_address[0]
        signed = self.users.sign_in(
            str(body.get("name", "")),
            str(body.get("password", "")),
            client=client,
            device=str(body.get("device") or "Browser")[:80],
        )
        days = SESSION_DAYS * 24 * 60 * 60
        self._json(
            200,
            {**_me(Caller(signed.user, False)), "token": signed.token, "expires": signed.expires},
            {"Set-Cookie": self._cookie(signed.token, days)},
        )

    def _cookie(self, token: str, max_age: int) -> str:
        """The browser's session cookie: script cannot read it, other sites cannot send
        it, and through the TLS proxy it travels only over TLS."""
        secure = "; Secure" if self.headers.get("Origin", "").startswith("https://") else ""
        return (
            f"{SESSION_COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; "
            f"SameSite=Strict{secure}"
        )

    def _v1(self, method: str) -> None:
        """Serve one versioned book-server resource (#357)."""
        write = method != "GET"
        # A DELETE may carry no body at all; any body it does carry must be JSON.
        bodiless = method == "DELETE" and not self.headers.get_all("Content-Length")
        if not self._trusted_api_request(write=write and not bodiless):
            if write:
                self._discard_body()
            self._refuse()
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
            self._refuse()
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

    def _v1_upload(self, transaction: str | None) -> None:
        """A file's bytes for the book server: an import file (``transaction`` None,
        ``upload_import_file``) or a document for one transaction (``attach_content``).
        """
        limit = MAX_UPLOAD_BODY if transaction is None else MAX_ATTACHMENT_BYTES
        if (
            not self._trusted_api_request()
            or self.headers.get_content_type() != "application/octet-stream"
        ):
            self._discard_body(limit)
            self._refuse()
            return
        try:
            query = QueryParams(urlparse(self.path).query)
            filename = query.text("filename", required=True) or ""
            query.finish()
        except QueryError as exc:
            self._discard_body(limit)
            self._error(400, exc.code, exc.fields)
            return
        try:
            length = self._content_length(limit)
            if length is None:
                return
            content = self.rfile.read(length)
            if len(content) != length:
                self._error(400, "request.body.incomplete", ("file",))
                return
            book = self.api_object.book

            def perform() -> tuple[object, int]:
                if transaction is None:
                    result: object = {"path": book.upload_import_file(filename, content)}
                else:
                    changed = book.attach_content(unquote(transaction), filename, content)
                    result = {"transaction": to_data(changed)}
                return result, book.revision

            result, revision = self._exclusive(perform)
            self._json(200, result, self._revision_headers(revision))
        except ResourceError as exc:
            self._error(exc.status, exc.code, exc.fields, message=exc.message)
        except ValueError as exc:
            self._error(400, "request.invalid", message=str(exc))
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._unexpected()

    def _import_upload(self) -> None:
        """Receive one bounded file without JSON encoding or a client filesystem path."""
        if (
            not self._trusted_api_request()
            or self.headers.get_content_type() != "application/octet-stream"
        ):
            self._discard_body(MAX_UPLOAD_BODY)
            self._refuse()
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
            self._refuse()
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
            self._refuse()
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
    #: Marks requests from programs on this computer (see ``Handler._local_request``).
    local_token: str
    #: Set on shutdown so open change streams end.
    stopping: threading.Event
    #: Each served book's path and the id a request names it by.
    books: dict[str, str]
    #: The books other commands asked this host to open; None where a host may not
    #: be joined (GTK shares only the book it has open).
    joined: JoinedBooks | None = None
    #: How books added later are run, as those given at the start.
    executor: Executor | None = None
    users: UserDirectory | None = None
    _changing = threading.Lock()

    def add_book(self, db: DbSQLite) -> str:
        """Serve one more book; the id requests name it by. ``ValueError`` if this
        host already serves it."""
        handler: type[Handler] = self.RequestHandlerClass  # type: ignore[assignment]
        with self._changing:
            file = _file_of(db)
            if file and any(_file_of(other.db) == file for other in handler.books.values()):
                raise ValueError(f"{db.path} is already served here")
            book = _hosted(db, self.executor, self.users)
            if book.key in handler.books:
                raise ValueError(f"{db.path} is already served here")
            # A new table, never one changed in place: requests read it without a lock.
            handler.books = {**handler.books, book.key: book}
            self.books = {**self.books, db.path or "": book.key}
        return book.key

    def remove_book(self, key: str) -> bool:
        """Stop serving a book once its writer work in progress is done; the caller
        closes it. The host's last book stays."""
        handler: type[Handler] = self.RequestHandlerClass  # type: ignore[assignment]
        with self._changing:
            book = handler.books.get(key)
            if book is None or len(handler.books) == 1:
                return False
            book.executor(lambda: None)
            handler.books = {name: each for name, each in handler.books.items() if name != key}
            self.books = {path: name for path, name in self.books.items() if name != key}
        return True

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


def _file_of(db: DbSQLite) -> str:
    """The book's file, resolved; "" for a book in memory."""
    return str(Path(db.path).resolve()) if db.path and db.path != ":memory:" else ""


def _hosted(db: DbSQLite, executor: Executor | None, users: UserDirectory | None) -> _Book:
    api_object = Api(db)
    # The book's permanent id, made now in a writable book; the host lists the book
    # under it, and grants name it (decision 0004).
    book_id = api_object.server.book_id()
    if users is not None and book_id and db.path and db.path != ":memory:":
        try:
            users.remember_book(book_id, str(Path(db.path).resolve()))
        except (OSError, sqlite3.Error):
            LOG.exception("could not note the book in the user directory")
    lock = threading.Lock()

    def locked(work: Callable[[], Any]) -> Any:
        with lock:
            return work()

    return _Book(
        key=book_id or f"unnamed-{secrets.token_hex(8)}",
        book_id=book_id,
        name=hosted_name(db.path),
        api=api_object,
        db=db,
        lock=lock,
        executor=executor or locked,
    )


def build_handler(
    db: DbSQLite | Sequence[DbSQLite],
    token: str,
    executor: Executor | None = None,
    trusted_origins: frozenset[str] = frozenset(),
    local_token: str = "",
    users: UserDirectory | None = None,
) -> type[Handler]:
    """Bind each book's serialized writer and short-lived read snapshots to a handler.

    ``db`` is one book or several (decision 0004): a request names the one it is for.
    Each book's writer work runs under its own lock in the request's thread unless
    ``executor`` runs it elsewhere (GTK runs it on its main loop, #359).
    """
    books: dict[str, _Book] = {}
    files: set[str] = set()
    for each in [db] if isinstance(db, DbSQLite) else db:
        file = _file_of(each)
        if file and file in files:
            raise ValueError(f"{each.path} is already served here")
        book = _hosted(each, executor, users)
        if book.key in books:
            raise ValueError(f"{each.path} is already served here")
        files.add(file)
        books[book.key] = book
    if not books:
        raise ValueError("a host serves at least one book")
    return type(
        "BoundHandler",
        (Handler,),
        {
            "books": books,
            "token": token,
            "trusted_origins": trusted_origins,
            "local_token": local_token,
            "users": users,
        },
    )


def serve(
    db: DbSQLite | Sequence[DbSQLite],
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    executor: Executor | None = None,
    *,
    token: str | None = None,
    trusted_origins: tuple[str, ...] = (),
    users: UserDirectory | None = None,
) -> BreadSchedHTTPServer:
    """Start the server for one book or several. Returns it without blocking; call
    ``serve_forever``. ``server.books`` maps each book's path to its id.

    ``token`` is a kept token (``token_from_file``), else a new one is made.
    ``trusted_origins`` are the public origins of a TLS proxy in front of it.
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the web interface binds to loopback only")
    origins = frozenset(trusted_origin(origin) for origin in trusted_origins)
    token = token or secrets.token_urlsafe(32)
    # Never written to a token file or printed: it reaches only the server record
    # (user-only) and the page this server opens itself (#358).
    local_token = secrets.token_urlsafe(32)
    handler = build_handler(
        db,
        token,
        executor,
        trusted_origins=origins,
        local_token=local_token,
        users=users if users is not None else UserDirectory(),
    )
    server = BreadSchedHTTPServer((host, port), handler)
    server.books = {book.db.path or "": book.key for book in handler.books.values()}
    server.executor = executor
    server.users = handler.users
    server.token = token
    server.local_token = local_token
    server.stopping = threading.Event()
    if open_browser:  # pragma: no cover - depends on a desktop session
        import webbrowser

        query = urlencode({"token": token, "local": local_token})
        threading.Timer(
            0.5,
            partial(webbrowser.open, f"http://{host}:{server.server_port}/#{query}"),
        ).start()
    return server
