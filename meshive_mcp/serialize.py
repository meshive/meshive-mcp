"""SDK dataclass → 모델용 dict.

`raw`(백엔드 원문)는 뺀다 — 크기만 키우고 정규화 필드와 중복이다. datetime 은 ISO 문자열.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_DROP = {"raw"}
# 소수점 또는 지수가 있는 숫자 문자열만 건드린다. "457" 같은 정수 문자열은 id 일 수 있어 그대로 둔다.
_DECIMAL_STR = re.compile(r"^-?(\d+\.\d+([eE][-+]?\d+)?|\d+[eE][-+]?\d+)$")


def normalize_number(value: str) -> str:
    """백엔드 Decimal 문자열을 사람이 읽는 형태로: "0E-8" → "0", "0.87741500" → "0.877415"."""
    if not _DECIMAL_STR.match(value):
        return value
    try:
        d = Decimal(value).normalize()
    except InvalidOperation:
        return value
    if d == 0:
        return "0"
    return format(d, "f")


def to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            if f.name in _DROP:
                continue
            out[f.name] = to_dict(getattr(obj, f.name))
        return out
    if isinstance(obj, dict):
        return {str(k): to_dict(v) for k, v in obj.items() if k not in _DROP}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return normalize_number(str(obj))
    if isinstance(obj, str):
        return normalize_number(obj)
    return obj
