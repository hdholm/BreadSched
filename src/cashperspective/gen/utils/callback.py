"""A tiny signal bus, mirroring Gramps' ``Callback`` mixin.

The database emits signals; views subscribe.  This is what keeps the core free of
any GTK dependency: the database never imports GObject, and the GTK layer never has
to poll.  Callbacks are held weakly for bound methods so a closed window cannot
keep a whole view tree alive, and one raising callback cannot stop the others.
"""

from __future__ import annotations

import inspect
import logging
import weakref
from collections.abc import Callable, Iterable
from typing import Any, ClassVar

LOG = logging.getLogger(__name__)

__all__ = ["Callback"]


class Callback:
    """Mixin providing ``connect`` / ``disconnect`` / ``emit``."""

    #: Signal name -> tuple describing the argument types, for documentation only.
    __signals__: ClassVar[dict[str, tuple | None]] = {}

    def __init__(self) -> None:
        self._callbacks: dict[str, list[tuple[int, Any]]] = {}
        self._next_signal_id = 1
        self._emit_depth = 0
        self._blocked = False

    # ------------------------------------------------------------ registration

    def _known_signals(self) -> dict[str, Any]:
        signals: dict[str, Any] = {}
        for klass in reversed(type(self).__mro__):
            signals.update(getattr(klass, "__signals__", {}) or {})
        return signals

    def connect(self, signal: str, callback: Callable[..., Any]) -> int:
        if signal not in self._known_signals():
            raise KeyError(f"unknown signal {signal!r}")
        handle = self._next_signal_id
        self._next_signal_id += 1
        if inspect.ismethod(callback) and callback.__self__ is not None:
            ref: Any = (
                weakref.ref(callback.__self__),
                callback.__func__.__name__,
            )
        else:
            ref = callback
        self._callbacks.setdefault(signal, []).append((handle, ref))
        return handle

    def disconnect(self, handle: int) -> None:
        for signal, entries in self._callbacks.items():
            remaining = [e for e in entries if e[0] != handle]
            if len(remaining) != len(entries):
                self._callbacks[signal] = remaining
                return

    def disconnect_all(self) -> None:
        self._callbacks.clear()

    # -------------------------------------------------------------- dispatching

    def block_signals(self, blocked: bool = True) -> None:
        """Temporarily silence emission, e.g. during a bulk import."""
        self._blocked = blocked

    def emit(self, signal: str, args: Iterable[Any] = ()) -> None:
        if self._blocked:
            return
        entries = self._callbacks.get(signal)
        if not entries:
            return
        payload = tuple(args)
        dead: list[int] = []
        for handle, ref in list(entries):
            func = self._resolve(ref)
            if func is None:
                dead.append(handle)
                continue
            try:
                func(*payload)
            except Exception:  # noqa: BLE001 - a bad listener must not break the DB
                LOG.exception("callback for signal %r failed", signal)
        for handle in dead:
            self.disconnect(handle)

    @staticmethod
    def _resolve(ref: Any) -> Callable[..., Any] | None:
        if isinstance(ref, tuple):
            weak, name = ref
            owner = weak()
            if owner is None:
                return None
            return getattr(owner, name, None)
        return ref
