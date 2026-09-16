"""Presentation-neutral success and validation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ServiceError:
    """A stable machine-readable failure returned by an application service."""

    code: str
    fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ServiceResult(Generic[T]):
    """Exactly one typed value or one-or-more structured validation failures."""

    value: T | None = None
    errors: tuple[ServiceError, ...] = ()

    def __post_init__(self) -> None:
        if (self.value is None) == (not self.errors):
            raise ValueError("a service result must contain either a value or errors")

    @property
    def ok(self) -> bool:
        return not self.errors

    @classmethod
    def success(cls, value: T) -> ServiceResult[T]:
        return cls(value=value)

    @classmethod
    def failure(cls, *errors: ServiceError) -> ServiceResult[T]:
        if not errors:
            raise ValueError("a failed service result needs at least one error")
        return cls(errors=tuple(errors))
