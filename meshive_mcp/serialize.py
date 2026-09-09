"""SDK dataclass → 모델용 dict.

`raw`(백엔드 원문)는 뺀다 — 크기만 키우고 정규화 필드와 중복이다. datetime 은 ISO 문자열.

금액 필드에는 `<필드>_display` 를 함께 싣는다("$0.068"). 원본 숫자는 모델이 합계를 계산할 때
필요하므로 그대로 두고, **보여줄 때는 display 를 쓰게** 한다 — 그래야 웹 콘솔과 같은 금액이
보인다(콘솔은 $/hr 3자리, 그 외 2자리). 규칙은 `money.py`, 소스는 콘솔 `Formatter.tsx`.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from . import money

_DROP = {"raw"}

# 시간당 요금은 어느 dataclass에 있든 필드명이 같다 → 이름 하나로 판정(콘솔 formatHourlyUsd, 3자리).
_HOURLY_FIELDS = {"price_per_hour"}
# 그 외 금액은 이름만으로는 구분이 안 된다(예: DailyCost.pod 는 금액, WorkspaceResources.pod 는 개수)
# → dataclass 이름과 함께 본다. 콘솔 formatUsd(2자리).
_USD_FIELDS: dict[str, set[str]] = {
    "Machine": {"earning_hourly"},                    # 콘솔 호스트 화면은 수익 /hr 도 2자리다
    "WorkspaceDetail": {"weekly_avg_daily_cost"},
    "DailyCost": {"pod", "storage", "serverless", "task", "asset"},
    "Credit": {"balance", "paid_balance", "bonus_balance",
               "auto_recharge_threshold", "auto_recharge_amount"},
    "CreditHistoryEntry": {"amount"},
    "Earnings": {"current_hourly", "daily", "accumulated_until_payout"},
    "Task": {"cost_so_far", "total_cost"},
    "AssetStorage": {"price_per_gb_month", "estimated_monthly_cost"},
    "StorageEstimate": {"price_per_gb_month"},
    "TaskEstimate": {"max_cost"},
}
# 파드 견적 내역은 {"gpu": "0.068", ...} 형태의 시간당 금액 묶음 — 콘솔 Receipt 도 줄마다 3자리다.
_HOURLY_DICT_FIELDS = {("PodEstimate", "breakdown")}
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
        cls = type(obj).__name__
        usd_fields = _USD_FIELDS.get(cls, frozenset())
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            if f.name in _DROP:
                continue
            value = getattr(obj, f.name)
            out[f.name] = to_dict(value)
            if f.name in _HOURLY_FIELDS:
                out[f"{f.name}_display"] = money.hourly(value)
            elif f.name in usd_fields:
                out[f"{f.name}_display"] = money.usd(value)
            elif (cls, f.name) in _HOURLY_DICT_FIELDS and isinstance(value, dict):
                out[f"{f.name}_display"] = {str(k): money.hourly(v) for k, v in value.items()}
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
