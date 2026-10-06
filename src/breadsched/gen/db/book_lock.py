"""The single-writer lock beside a native book.

Only one process may open a book for writing. The writer creates ``<book>.lock``
exclusively and records its PID, host, and a random token; a lock left by a process
that no longer exists on this host is stale and is replaced. Release removes the
file only while it still holds this writer's token, so a lock replaced by another
writer is never removed.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from .base import DbError

__all__ = ["BookWriterLock"]


@dataclass(slots=True)
class BookWriterLock:
    """A held writer lock; ``release`` gives it up."""

    path: Path
    token: str

    @staticmethod
    def lock_path(book: str) -> Path:
        resolved = Path(book).expanduser().resolve()
        return resolved.with_name(f"{resolved.name}.lock")

    @staticmethod
    def pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            # ``os.kill(pid, 0)`` is a harmless existence probe on POSIX. On
            # Windows, however, signal value 0 is CTRL_C_EVENT and can interrupt
            # the process whose liveness we are trying to inspect. Query a process
            # handle instead and treat access-denied or unexpected errors
            # conservatively as evidence that the process may still be alive.
            import ctypes

            win_dll = getattr(ctypes, "WinDLL", None)
            if win_dll is None:  # pragma: no cover - defensive Windows fallback
                return True
            kernel32 = win_dll("kernel32", use_last_error=True)
            process_query_limited_information = 0x1000
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            error_invalid_parameter = 87
            get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
            return get_last_error() != error_invalid_parameter
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    @classmethod
    def acquire(cls, book: str) -> BookWriterLock | None:
        """Take the writer lock for ``book``; ``None`` for an in-memory book.

        Raises ``DbError`` naming the owner when another live writer holds it.
        """
        if book == ":memory:":
            return None
        lock_path = cls.lock_path(book)
        hostname = socket.gethostname()
        token = os.urandom(16).hex()
        payload = {
            "pid": os.getpid(),
            "host": hostname,
            "token": token,
            "book": str(Path(book).expanduser().resolve()),
        }
        while True:
            try:
                fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                try:
                    existing = json.loads(lock_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    existing = {}
                owner_host = existing.get("host")
                owner_pid = existing.get("pid")
                stale = (
                    owner_host == hostname
                    and isinstance(owner_pid, int)
                    and not cls.pid_is_alive(owner_pid)
                )
                if stale:
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                owner = "another process"
                if owner_host and owner_pid:
                    owner = f"PID {owner_pid} on {owner_host}"
                raise DbError(
                    f"book is already open for writing by {owner}; "
                    "close that writer or open this book read-only"
                ) from None
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                return cls(lock_path, token)

    def release(self) -> None:
        """Remove the lock file if it is still this writer's."""
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if existing.get("token") != self.token:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
