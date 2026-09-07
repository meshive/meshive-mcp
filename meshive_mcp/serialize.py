"""SDK dataclass → 모델용 dict.

`raw`(백엔드 원문)는 뺀다 — 크기만 키우고 정규화 필드와 중복이다. datetime 은 ISO 문자열.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal
from typing import Any

_DROP = {"raw"}


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
        return str(obj)
    return obj
