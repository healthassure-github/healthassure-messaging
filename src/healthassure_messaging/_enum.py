from __future__ import annotations

from enum import Enum


class _StringEnum(str, Enum):
    """Python 3.10-compatible base with the string behavior of StrEnum."""

    def __str__(self) -> str:
        return str.__str__(self)
