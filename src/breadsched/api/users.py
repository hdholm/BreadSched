"""The people a book host serves, and the tokens they prove themselves with (0004).

**Experimental.** A *book host* (every book server one computer account runs) keeps
one user directory, ``users.sqlite`` in the account's data folder, never in a book:
a book file may be copied or synced and must not carry anyone's credentials.

Phase 1 of decision 0004 knows one user, the computer account itself (its
administrator), and *device tokens*. A program run by that account on this computer
proves who it is with the device token kept in its private config folder
(``device-token``, ``0600``), made the first time it is needed; that token may also
name files on this computer (``host_files``), as only the account's own programs may
(#358). The host stores a token only as its SHA-256 digest (tokens are random, so a
slow hash adds nothing), with when it was made and last used, so it can be listed
and revoked.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import secrets
import socket
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..gen.utils.user_paths import config_directory, data_directory

__all__ = [
    "Caller",
    "DeviceToken",
    "User",
    "UserDirectory",
    "device_token",
    "device_token_path",
    "users_path",
]

#: Characters of randomness in a token (``secrets.token_urlsafe``).
_TOKEN_BYTES = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    administrator INTEGER NOT NULL DEFAULT 0,
    host_account INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    host_files INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    last_used TEXT
);
"""


def users_path() -> Path:
    """Where this computer account's user directory is kept."""
    return data_directory() / "users.sqlite"


def device_token_path() -> Path:
    """Where this computer account keeps its own device token."""
    return config_directory() / "device-token"


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    display_name: str
    administrator: bool
    #: The computer account that runs the host.
    host_account: bool


@dataclass(frozen=True, slots=True)
class Caller:
    """Who a request comes from, and whether it may name files on this computer."""

    user: User
    host_files: bool


@dataclass(frozen=True, slots=True)
class DeviceToken:
    """A token as the directory lists it (never the token itself)."""

    id: str
    user: str
    name: str
    host_files: bool
    created: str
    last_used: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _private_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


class UserDirectory:
    """The users and device tokens of one computer account's book host."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else users_path()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            _private_parent(self.path)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(_SCHEMA)
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _user(row: tuple) -> User:
        return User(row[0], row[1], row[2], bool(row[3]), bool(row[4]))

    def host_user(self) -> User:
        """The computer account's own user, made (as the administrator) if absent."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, name, display_name, administrator, host_account FROM users"
                " WHERE host_account = 1"
            ).fetchone()
            if row is not None:
                return self._user(row)
            name = getpass.getuser()
            user = User(uuid.uuid4().hex, name, name, True, True)
            connection.execute(
                "INSERT INTO users VALUES (?, ?, ?, 1, 1, ?)", (user.id, name, name, _now())
            )
            return user

    def users(self) -> tuple[User, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, name, display_name, administrator, host_account FROM users"
                " ORDER BY name"
            ).fetchall()
        return tuple(self._user(row) for row in rows)

    def issue_token(self, user: User, name: str, *, host_files: bool = False) -> str:
        """A new token for ``user``; only its digest is kept, so this is the one
        chance to see it."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (uuid.uuid4().hex, user.id, name, _digest(token), int(host_files), _now()),
            )
        return token

    def authenticate(self, token: str) -> Caller | None:
        """Who ``token`` belongs to, or None for a token this host never issued (or
        has revoked)."""
        if not token:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT u.id, u.name, u.display_name, u.administrator, u.host_account,"
                " t.id, t.host_files FROM tokens t JOIN users u ON u.id = t.user_id"
                " WHERE t.digest = ?",
                (_digest(token),),
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE tokens SET last_used = ? WHERE id = ?", (_now(), row[5]))
        return Caller(self._user(row[:5]), bool(row[6]))

    def tokens(self, user: User) -> tuple[DeviceToken, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, user_id, name, host_files, created, last_used FROM tokens"
                " WHERE user_id = ? ORDER BY created",
                (user.id,),
            ).fetchall()
        return tuple(
            DeviceToken(row[0], row[1], row[2], bool(row[3]), row[4], row[5]) for row in rows
        )

    def revoke(self, token_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute("DELETE FROM tokens WHERE id = ?", (token_id,)).rowcount > 0


def device_token(directory: UserDirectory | None = None, path: Path | None = None) -> str:
    """This computer account's own device token, made the first time it is needed.

    It is kept ``0600`` in the account's config folder, beside its preferences, and
    replaced if the host no longer knows it (revoked, or the directory was reset).
    """
    directory = directory if directory is not None else UserDirectory()
    target = Path(path) if path is not None else device_token_path()
    try:
        kept = target.read_text(encoding="utf-8").strip()
    except OSError:
        kept = ""
    if kept and directory.authenticate(kept) is not None:
        return kept
    user = directory.host_user()
    token = directory.issue_token(user, f"{socket.gethostname()} ({user.name})", host_files=True)
    _private_parent(target)
    temporary = target.with_name(f".{target.name}.{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.replace(temporary, target)
    return token
