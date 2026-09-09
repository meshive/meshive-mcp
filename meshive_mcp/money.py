"""금액 → 사람이 읽는 문자열. **웹 콘솔과 같은 값**을 낸다.

규칙의 소스는 `WebFrontend/src/common/Formatter.tsx` 다:

  시간당 요금($/hr)  → `formatHourlyUsd` = 3자리 고정 ("$0.068")
  그 외 금액          → `formatUsd`       = 2자리        ("$2.10")

3자리 고정인 이유는 그 파일 주석이 적어 둔 그대로다 — 화면마다 반올림이 다르면($0.07 vs $0.065)
유저가 청구 금액을 신뢰하지 못한다. 콘솔·CLI·MCP 가 같은 값을 보여야 한다.

반올림은 float 포맷이 아니라 Decimal + ROUND_HALF_UP 으로 한다. 콘솔의 Intl.NumberFormat 기본
반올림이 halfExpand(0에서 먼 쪽)이고, 파이썬 float 포맷은 half-even + 이진 오차라 경계값에서
갈린다(0.015 → "$0.01" vs "$0.02").
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

MISSING = "-"


def _fmt(value: Any, digits: int) -> str:
    if value is None or value == "":
        return MISSING
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return MISSING
    if not amount.is_finite():
        return MISSING
    quantized = amount.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    # 환급/회수 원장행은 음수 — "$-12.50" 이 아니라 "-$12.50".
    sign = "-" if quantized < 0 else ""
    return f"{sign}${abs(quantized):,.{digits}f}"


def hourly(value: Any) -> str:
    """시간당 요금 → "$0.068" (3자리 고정). 콘솔 `formatHourlyUsd`."""
    return _fmt(value, 3)


def usd(value: Any) -> str:
    """그 외 금액 → "$2.10" (2자리). 콘솔 `formatUsd`."""
    return _fmt(value, 2)
