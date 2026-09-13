"""Cooperative cancellation shared by engines, importers, and presentations."""

from __future__ import annotations

__all__ = ["OperationCancelled"]


class OperationCancelled(Exception):
    """A cooperative operation was cancelled before completion."""
