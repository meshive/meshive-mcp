"""List capping + opaque cursors.

The cursor is base64url("o=<offset>"). The format is fixed from the start so that if the backend later
supports limit/cursor, the server passes the same string through and nothing changes for the model.
"""
from __future__ import annotations

import base64
from typing import Any, Sequence

from .errors import invalid_argument
from .settings import settings


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o={offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        key, _, value = raw.partition("=")
        if key != "o":
            raise ValueError
        offset = int(value)
        if offset < 0:
            raise ValueError
        return offset
    except (ValueError, UnicodeDecodeError):
        raise invalid_argument("Invalid cursor. Use the next_cursor value from the previous response.") from None


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return settings.default_limit
    if limit < 1:
        raise invalid_argument("limit must be at least 1.")
    return min(limit, settings.max_limit)


def paginate(items: Sequence[Any], limit: int | None, cursor: str | None, *,
             total: int | None = None) -> dict[str, Any]:
    """Cut a full in-memory list down to one page.

    When total is given (a server-paged result), items is already one page and only the offset moves forward.
    """
    size = clamp_limit(limit)
    offset = decode_cursor(cursor)
    if total is None:
        total = len(items)
        page = list(items[offset:offset + size])
    else:
        page = list(items)
    next_offset = offset + len(page)
    return {
        "items": page,
        "total": total,
        "next_cursor": encode_cursor(next_offset) if next_offset < total else None,
    }
