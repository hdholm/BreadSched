"""Where a point of one widget lies in another, on any GTK 4."""

from __future__ import annotations

from ..gi_setup import Graphene, Gtk

__all__ = ["point_in"]


def point_in(
    source: Gtk.Widget, target: Gtk.Widget, x: float, y: float
) -> tuple[float, float] | None:
    """``(x, y)`` of ``source`` in ``target``'s coordinates, or None when the two
    share no window. GTK 4.12 replaced ``translate_coordinates`` with
    ``compute_point``; either is used, whichever this GTK has."""
    compute = getattr(source, "compute_point", None)
    if compute is None:
        found, moved_x, moved_y = source.translate_coordinates(target, x, y)
        return (moved_x, moved_y) if found else None
    point = Graphene.Point()
    point.x, point.y = x, y
    found, moved = compute(target, point)
    return (moved.x, moved.y) if found else None
