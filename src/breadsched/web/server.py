"""A locally hosted web interface.

Built on :mod:`http.server` with no framework, for the same reason the core has no
third-party dependencies: a finance tool that people run on their own machine for
years should not rot because a web framework moved on. The whole surface is a small
JSON API plus one static page.

It binds to loopback only and still treats browser requests as untrusted input.
Every API request carries an unguessable per-server token, writes must be JSON, and
Host/Origin checks reject cross-site and DNS-rebinding requests.

The pieces: ``web.transport`` owns HTTP, ``web.resources`` maps each route to a
resource adapter in ``web/*_resource.py``, and each adapter receives the open book
as an :class:`~breadsched.web.context.Api` context. This module re-exports the entry
points.
"""

from __future__ import annotations

from .context import Api, api
from .transport import build_handler, serve

__all__ = ["Api", "api", "build_handler", "serve"]
