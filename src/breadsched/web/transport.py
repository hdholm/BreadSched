"""Authenticated loopback HTTP transport for the web resource adapters."""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from ..gen.db.sqlite import DbSQLite
from ..gen.lib import Money
from ..gen.utils.logs import get_logger
from .resources import GET_ROUTES, POST_ROUTES, QueryError, QueryParams, ResourceError
from .server import Api

LOG = get_logger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_JSON_BODY = 64 * 1024


def _encode(value: object) -> object:
    if isinstance(value, Money):
        return str(value.to_decimal())
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


class Handler(BaseHTTPRequestHandler):
    """Serve static files and dispatch authenticated JSON resource calls."""

    server_version = "BreadSched"
    api_object: Api
    lock: threading.Lock
    token: str

    _LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - base class name
        LOG.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, default=_encode).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

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
        path = (STATIC / name).resolve()
        if not path.is_file() or STATIC.resolve() not in path.parents:
            self._error(404, "resource.not_found")
            return
        kind = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), kind)

    def _trusted_host(self) -> bool:
        host = urlparse(f"//{self.headers.get('Host', '')}").hostname
        return host in self._LOCAL_HOSTS

    def _trusted_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
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
        LOG.exception("request %s failed: %s", correlation_id, self.path)
        self._error(500, "internal.error", correlation_id=correlation_id)

    def do_GET(self) -> None:  # noqa: N802 - required by the base class
        if not self._trusted_host():
            self._error(403, "request.host.untrusted", ("Host",))
            return
        parsed = urlparse(self.path)
        route = GET_ROUTES.get(parsed.path)
        if route is None:
            self._static("index.html" if parsed.path in ("/", "") else parsed.path[1:])
            return
        if not self._trusted_api_request():
            self._error(403, "request.untrusted")
            return
        try:
            query = QueryParams(parsed.query)
            with self.lock:
                result = route(self.api_object, query)
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

    def _content_length(self) -> int | None:
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
        if length > MAX_JSON_BODY:
            self._error(413, "request.body.too_large", ("body",))
            return None
        return length

    def do_POST(self) -> None:  # noqa: N802 - required by the base class
        if not self._trusted_api_request(write=True):
            self._error(403, "request.untrusted")
            return
        parsed = urlparse(self.path)
        route = POST_ROUTES.get(parsed.path)
        if route is None:
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
        if not isinstance(body, Mapping):
            self._error(400, "request.body.object_required", ("body",))
            return
        try:
            with self.lock:
                result = route(self.api_object, body)
            self._json(200, result)
        except ResourceError as exc:
            self._error(exc.status, exc.code, exc.fields, message=exc.message)
        except KeyError:
            self._error(400, "request.reference.not_found")
        except ValueError as exc:
            self._error(400, "request.invalid", message=str(exc))
        except Exception:  # noqa: BLE001 - isolate the threaded server
            self._unexpected()


class BreadSchedHTTPServer(ThreadingHTTPServer):
    """Threaded local server carrying the API token exposed to the launcher."""

    token: str


def build_handler(db: DbSQLite, token: str) -> type[Handler]:
    """Bind one serialized writer connection and token to a handler class."""
    return type(
        "BoundHandler",
        (Handler,),
        {"api_object": Api(db), "lock": threading.Lock(), "token": token},
    )


def serve(
    db: DbSQLite,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> BreadSchedHTTPServer:
    """Start the server. Returns it without blocking; call ``serve_forever``."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the web interface binds to loopback only")
    token = secrets.token_urlsafe(32)
    server = BreadSchedHTTPServer((host, port), build_handler(db, token))
    server.token = token
    if open_browser:  # pragma: no cover - depends on a desktop session
        import webbrowser

        query = urlencode({"token": token})
        threading.Timer(
            0.5,
            partial(webbrowser.open, f"http://{host}:{server.server_port}/#{query}"),
        ).start()
    return server
