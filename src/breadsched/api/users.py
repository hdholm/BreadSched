"""The people a book host serves, and how they prove who they are (decision 0004).

**Experimental.** A *book host* (every book server one computer account runs) keeps
one user directory, ``users.sqlite`` in the account's data folder, never in a book:
a book file may be copied or synced and must not carry anyone's credentials.

- **The computer account** is the host's first user and its administrator, made the
  first time it is needed. Its programs prove who they are with its *device token*,
  kept ``0600`` in its config folder (``device-token``) and made when first needed;
  that token may also name files on this computer (``host_files``, #358).
- **Other users** are made by an administrator, with a temporary password they must
  change when they first sign in. Passwords are kept as ``scrypt`` hashes (the
  standard library; no dependency) with their salt and parameters.
- **Tokens** are random and kept only as SHA-256 digests (a slow hash adds nothing
  to a random token), with when they were made and last used. A *device* token lasts
  until it is revoked; a *session* from signing in ends after
  :data:`SESSION_DAYS` without use, and when its user's password changes.
- **Signing in is throttled:** after :data:`FREE_FAILURES` failures for one name or
  from one address, each further attempt must wait twice as long as the last (up to
  :data:`MAX_DELAY`). A failure never says whether the name exists.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import os
import re
import secrets
import socket
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..gen.utils.user_paths import config_directory, data_directory

__all__ = [
    "Caller",
    "DeviceToken",
    "FREE_FAILURES",
    "MAX_DELAY",
    "MIN_PASSWORD_LENGTH",
    "SESSION_DAYS",
    "SignedIn",
    "User",
    "UserDirectory",
    "UserError",
    "device_token",
    "device_token_path",
    "users_path",
]

#: Bytes of randomness in a token (``secrets.token_urlsafe``).
_TOKEN_BYTES = 32
#: A session ends after this many days without use.
SESSION_DAYS = 30
#: The shortest password accepted.
MIN_PASSWORD_LENGTH = 10
#: Failures allowed before signing in must wait.
FREE_FAILURES = 5
#: The longest wait between attempts.
MAX_DELAY = timedelta(minutes=15)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: scrypt cost: 16 MiB and well under a second per attempt on a household computer.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    administrator INTEGER NOT NULL DEFAULT 0,
    host_account INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    password TEXT,
    must_change INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    host_files INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    last_used TEXT,
    kind TEXT NOT NULL DEFAULT 'device',
    expires TEXT
);
CREATE TABLE IF NOT EXISTS failures (
    key TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    last TEXT NOT NULL
);
"""
_USER_COLUMNS = "id, name, display_name, administrator, host_account, password, must_change"


def users_path() -> Path:
    """Where this computer account's user directory is kept."""
    return data_directory() / "users.sqlite"


def device_token_path() -> Path:
    """Where this computer account keeps its own device token."""
    return config_directory() / "device-token"


class UserError(Exception):
    """A refused account or sign-in request, with a stable code."""

    def __init__(self, code: str, fields: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.code = code
        self.fields = fields


@dataclass(frozen=True, slots=True)
class User:
    id: str
    name: str
    display_name: str
    administrator: bool
    #: The computer account that runs the host.
    host_account: bool
    has_password: bool = False
    #: Signed in with a temporary password that must be changed.
    must_change: bool = False


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
    kind: str = "device"
    expires: str | None = None


@dataclass(frozen=True, slots=True)
class SignedIn:
    """A session made by signing in; ``token`` is shown only this once."""

    user: User
    token: str
    expires: str


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _text(when: datetime) -> str:
    return when.isoformat()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    encode = base64.b64encode
    return "scrypt${n}${r}${p}${salt}${hash}".format(
        **_SCRYPT, salt=encode(salt).decode(), hash=encode(derived).decode()
    )


def _password_matches(password: str, stored: str | None) -> bool:
    if not stored:
        # Spend the same effort, so a missing name or password is not told apart.
        _hash_password(password)
        return False
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(expected)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, base64.b64decode(expected))


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError("users.password.too_short", ("password",))


def _private_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


class UserDirectory:
    """The users and tokens of one computer account's book host."""

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
        return User(row[0], row[1], row[2], bool(row[3]), bool(row[4]), bool(row[5]), bool(row[6]))

    def _find(self, connection: sqlite3.Connection, name: str) -> tuple | None:
        return connection.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE name = ?", (name,)
        ).fetchone()

    # ----------------------------------------------------------------- users

    def host_user(self) -> User:
        """The computer account's own user, made (as the administrator) if absent."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE host_account = 1"
            ).fetchone()
            if row is not None:
                return self._user(row)
            name = getpass.getuser()
            user = User(uuid.uuid4().hex, name, name, True, True)
            connection.execute(
                "INSERT INTO users (id, name, display_name, administrator, host_account,"
                " created) VALUES (?, ?, ?, 1, 1, ?)",
                (user.id, name, name, _text(_now())),
            )
            return user

    def users(self) -> tuple[User, ...]:
        self.host_user()
        with self._connect() as connection:
            rows = connection.execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY name").fetchall()
        return tuple(self._user(row) for row in rows)

    def user(self, name: str) -> User:
        with self._connect() as connection:
            row = self._find(connection, name)
        if row is None:
            raise UserError("users.user.not_found", ("name",))
        return self._user(row)

    def add_user(
        self,
        name: str,
        password: str,
        *,
        display_name: str = "",
        administrator: bool = False,
    ) -> User:
        """A new user with a temporary password, to be changed at first sign-in."""
        if not _NAME.match(name):
            raise UserError("users.name.invalid", ("name",))
        _check_password(password)
        self.host_user()
        user = User(uuid.uuid4().hex, name, display_name.strip() or name, administrator, False)
        with self._connect() as connection:
            if self._find(connection, name) is not None:
                raise UserError("users.name.taken", ("name",))
            connection.execute(
                "INSERT INTO users (id, name, display_name, administrator, host_account,"
                " created, password, must_change) VALUES (?, ?, ?, ?, 0, ?, ?, 1)",
                (
                    user.id,
                    name,
                    user.display_name,
                    int(administrator),
                    _text(_now()),
                    _hash_password(password),
                ),
            )
        return self.user(name)

    def remove_user(self, name: str) -> None:
        """Remove a user, their tokens and sessions with them. The computer account
        and the last administrator stay."""
        user = self.user(name)
        if user.host_account:
            raise UserError("users.user.host_account", ("name",))
        with self._connect() as connection:
            if user.administrator:
                others = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE administrator = 1 AND id != ?",
                    (user.id,),
                ).fetchone()[0]
                if not others:
                    raise UserError("users.user.last_administrator", ("name",))
            connection.execute("DELETE FROM users WHERE id = ?", (user.id,))

    def set_password(self, name: str, new: str, *, current: str | None = None) -> User:
        """Change a user's own password. ``current`` is required once there is one;
        every session of the user ends."""
        _check_password(new)
        with self._connect() as connection:
            row = self._find(connection, name)
            if row is None:
                raise UserError("users.user.not_found", ("name",))
            stored = connection.execute(
                "SELECT password FROM users WHERE id = ?", (row[0],)
            ).fetchone()[0]
            if stored and not _password_matches(current or "", stored):
                raise UserError("users.password.wrong", ("current",))
            connection.execute(
                "UPDATE users SET password = ?, must_change = 0 WHERE id = ?",
                (_hash_password(new), row[0]),
            )
            connection.execute(
                "DELETE FROM tokens WHERE user_id = ? AND kind = 'session'", (row[0],)
            )
        return self.user(name)

    def reset_password(self, name: str, temporary: str) -> User:
        """An administrator's reset: a temporary password the user must change, and
        every session of the user ends."""
        _check_password(temporary)
        user = self.user(name)
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET password = ?, must_change = 1 WHERE id = ?",
                (_hash_password(temporary), user.id),
            )
            connection.execute(
                "DELETE FROM tokens WHERE user_id = ? AND kind = 'session'", (user.id,)
            )
        return self.user(name)

    # ---------------------------------------------------------------- tokens

    def issue_token(self, user: User, name: str, *, host_files: bool = False) -> str:
        """A new device token for ``user``; only its digest is kept, so this is the
        one chance to see it."""
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tokens (id, user_id, name, digest, host_files, created, kind)"
                " VALUES (?, ?, ?, ?, ?, ?, 'device')",
                (uuid.uuid4().hex, user.id, name, _digest(token), int(host_files), _text(_now())),
            )
        return token

    def authenticate(self, token: str, *, now: datetime | None = None) -> Caller | None:
        """Who ``token`` belongs to, or None for a token this host never issued, has
        revoked, or whose session has lapsed. Using a session keeps it alive."""
        if not token:
            return None
        moment = now or _now()
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {', '.join('u.' + c for c in _USER_COLUMNS.split(', '))},"
                " t.id, t.host_files, t.kind, t.expires"
                " FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.digest = ?",
                (_digest(token),),
            ).fetchone()
            if row is None:
                return None
            token_id, host_files, kind, expires = row[7:]
            if expires is not None and datetime.fromisoformat(expires) <= moment:
                connection.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
                return None
            renewed = _text(moment + timedelta(days=SESSION_DAYS)) if kind == "session" else None
            connection.execute(
                "UPDATE tokens SET last_used = ?, expires = COALESCE(?, expires) WHERE id = ?",
                (_text(moment), renewed, token_id),
            )
        return Caller(self._user(row[:7]), bool(host_files))

    def tokens(self, user: User) -> tuple[DeviceToken, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, user_id, name, host_files, created, last_used, kind, expires"
                " FROM tokens WHERE user_id = ? ORDER BY created",
                (user.id,),
            ).fetchall()
        return tuple(
            DeviceToken(row[0], row[1], row[2], bool(row[3]), row[4], row[5], row[6], row[7])
            for row in rows
        )

    def revoke(self, token_id: str, user: User | None = None) -> bool:
        """Revoke a token (only one of ``user``'s, when given)."""
        with self._connect() as connection:
            if user is None:
                cursor = connection.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            else:
                cursor = connection.execute(
                    "DELETE FROM tokens WHERE id = ? AND user_id = ?", (token_id, user.id)
                )
            return cursor.rowcount > 0

    def sign_out(self, token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM tokens WHERE digest = ? AND kind = 'session'", (_digest(token),)
            )
            return cursor.rowcount > 0

    # --------------------------------------------------------------- signing in

    def _wait(self, connection: sqlite3.Connection, key: str, moment: datetime) -> timedelta:
        row = connection.execute(
            "SELECT count, last FROM failures WHERE key = ?", (key,)
        ).fetchone()
        if row is None or row[0] < FREE_FAILURES:
            return timedelta(0)
        delay = min(MAX_DELAY, timedelta(seconds=2 ** (row[0] - FREE_FAILURES)))
        return max(timedelta(0), datetime.fromisoformat(row[1]) + delay - moment)

    def sign_in(
        self,
        name: str,
        password: str,
        *,
        client: str,
        device: str = "Signed in",
        now: datetime | None = None,
    ) -> SignedIn:
        """A session for ``name``, or ``signin.failed`` (whatever was wrong) or
        ``signin.throttled`` while too many recent attempts failed. ``client`` is
        the address the attempt came from."""
        moment = now or _now()
        keys = (f"name:{name.casefold()}", f"address:{client}")
        with self._connect() as connection:
            if any(self._wait(connection, key, moment) for key in keys):
                raise UserError("signin.throttled", ())
            row = self._find(connection, name)
            stored = None
            if row is not None:
                stored = connection.execute(
                    "SELECT password FROM users WHERE id = ?", (row[0],)
                ).fetchone()[0]
            # Checked even for a name that does not exist, at the same cost.
            matches = _password_matches(password, stored)
            if row is None or not matches:
                for key in keys:
                    connection.execute(
                        "INSERT INTO failures VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE"
                        " SET count = count + 1, last = excluded.last",
                        (key, _text(moment)),
                    )
                failed = True
            else:
                failed = False
                connection.executemany("DELETE FROM failures WHERE key = ?", [(k,) for k in keys])
                token = secrets.token_urlsafe(_TOKEN_BYTES)
                expires = _text(moment + timedelta(days=SESSION_DAYS))
                connection.execute(
                    "INSERT INTO tokens (id, user_id, name, digest, host_files, created,"
                    " kind, expires) VALUES (?, ?, ?, ?, 0, ?, 'session', ?)",
                    (uuid.uuid4().hex, row[0], device, _digest(token), _text(moment), expires),
                )
        # Raised outside the block, so the failure counts are committed.
        if failed or row is None:
            raise UserError("signin.failed", ())
        return SignedIn(self._user(row), token, expires)


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
