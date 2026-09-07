"""목록 캡핑 + 불투명 커서.

커서는 base64url("o=<offset>"). 형식을 처음부터 고정해 두는 이유: 나중에 백엔드가
limit/cursor 를 지원하면 서버가 같은 문자열을 그대로 넘기고, 모델 입장에서는 아무것도 안 바뀐다.
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
    """메모리 안의 전체 목록을 한 페이지로 자른다.

    total 을 주면(서버 페이징 결과) items 는 이미 한 페이지로 보고 offset 만 앞으로 옮긴다.
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
