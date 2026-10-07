"""Amounts → human-readable strings. Produces **the same values as the web console**.

The rules follow the web console:

  hourly rates ($/hr)  → 3 decimals, always ("$0.068")
  other amounts        → 2 decimals         ("$2.10")

Why 3 decimals, always: if screens round differently ($0.07 vs $0.065),
users can't trust what they're billed. The console, CLI and MCP must show the same values.

Rounding uses Decimal + ROUND_HALF_UP, not float formatting. The console's Intl.NumberFormat rounds
halfExpand (away from zero) by default, while Python float formatting is half-even plus binary error, so they
differ at boundaries (0.015 → "$0.01" vs "$0.02").
"""
from __future__ import annotations

from decimal import Decimal, DecimalException, ROUND_HALF_UP
from typing import Any

MISSING = "-"


def _fmt(value: Any, digits: int) -> str:
    if value is None or value == "":
        return MISSING
    try:
        amount = Decimal(str(value))
    except (DecimalException, TypeError, ValueError):
        return MISSING
    if not amount.is_finite() or abs(amount.adjusted()) > 64:
        return MISSING
    try:
        quantized = amount.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    except DecimalException:
        return MISSING
    # Refund/clawback ledger rows are negative — "-$12.50", not "$-12.50".
    sign = "-" if quantized < 0 else ""
    return f"{sign}${abs(quantized):,.{digits}f}"


def hourly(value: Any) -> str:
    """Hourly rate → "$0.068" (always 3 decimals). Same as the console."""
    return _fmt(value, 3)


def usd(value: Any) -> str:
    """Any other amount → "$2.10" (2 decimals). Same as the console."""
    return _fmt(value, 2)
