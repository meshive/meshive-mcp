"""SDK dataclass → dict for the model.

`raw` (the backend's original) is dropped — it only adds size and duplicates the normalized fields. datetimes become ISO strings.

Amount fields come with a `<field>_display` ("$0.068"). The original number stays because the model needs it
to compute totals, but **it should use display when showing amounts** — that way the amounts match
the web console ($/hr with 3 decimals, everything else 2). The rules live in `money.py`.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import date, datetime
from decimal import Decimal, DecimalException
from typing import Any

from . import money

_DROP = {"raw"}
# Fields per dataclass that aren't sent to the model. Credit's paid/bonus are leftovers of the split from before free credits were retired (2026-10) —
# the server sends paid = balance, bonus = 0, and the SDK keeps the fields only for backward compatibility. Sending them makes the model explain a split that no longer exists.
_DROP_BY_CLASS: dict[str, set[str]] = {"Credit": {"paid_balance", "bonus_balance"}}

# Hourly rates have the same field name in every dataclass → decided by the name alone (3 decimals, like the console).
_HOURLY_FIELDS = {"price_per_hour", "storage_rate_per_hour", "price_cap_per_hour"}
# Other amounts can't be told apart by name alone (e.g. DailyCost.pod is an amount, WorkspaceResources.pod a count)
# → checked together with the dataclass name. 2 decimals, like the console.
_USD_FIELDS: dict[str, set[str]] = {
    "Machine": {"earning_hourly"},                    # the console's host screen shows earnings /hr with 2 decimals too
    "WorkspaceDetail": {"weekly_avg_daily_cost"},
    "DailyCost": {"pod", "storage", "serverless", "task", "asset"},
    "Credit": {"balance", "auto_recharge_threshold", "auto_recharge_amount"},
    "CreditHistoryEntry": {"amount"},
    "Earnings": {"current_hourly", "daily", "accumulated_until_payout"},
    "Task": {"cost_so_far", "total_cost"},
    "AssetStorage": {"price_per_gb_month", "estimated_monthly_cost"},
    "StorageEstimate": {"price_per_gb_month"},
    "TaskEstimate": {"max_cost"},
}
# A pod estimate breakdown is a set of hourly amounts like {"gpu": "0.068", ...} — the console's Receipt shows 3 decimals per line too.
_HOURLY_DICT_FIELDS = {("PodEstimate", "breakdown")}
# Only numeric strings with a decimal point or exponent are touched. Integer strings like "457" may be ids, so they stay as-is.
_DECIMAL_STR = re.compile(r"^-?(\d+\.\d+([eE][-+]?\d+)?|\d+[eE][-+]?\d+)$")


def normalize_number(value: str) -> str:
    """Backend Decimal strings in human-readable form: "0E-8" → "0", "0.87741500" → "0.877415"."""
    if len(value) > 128 or not _DECIMAL_STR.fullmatch(value):
        return value
    try:
        d = Decimal(value)
        if not d.is_finite() or abs(d.adjusted()) > 64 or abs(d.as_tuple().exponent) > 64:
            return value
    except DecimalException:
        return value
    if d == 0:
        return "0"
    fixed = format(d, "f")
    return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed


def to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        cls = type(obj).__name__
        usd_fields = _USD_FIELDS.get(cls, frozenset())
        drop = _DROP_BY_CLASS.get(cls, frozenset())
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            if f.name in _DROP or f.name in drop:
                continue
            value = getattr(obj, f.name)
            out[f.name] = to_dict(value)
            if f.name in _HOURLY_FIELDS:
                out[f.name] = normalize_number(value) if isinstance(value, str) else to_dict(value)
                out[f"{f.name}_display"] = money.hourly(value)
            elif f.name in usd_fields:
                out[f.name] = normalize_number(value) if isinstance(value, str) else to_dict(value)
                out[f"{f.name}_display"] = money.usd(value)
            elif (cls, f.name) in _HOURLY_DICT_FIELDS and isinstance(value, dict):
                out[f.name] = {str(k): normalize_number(v) if isinstance(v, str) else to_dict(v)
                               for k, v in value.items()}
                out[f"{f.name}_display"] = {str(k): money.hourly(v) for k, v in value.items()}
        # Secret connect values (ComfyUI's ACCESS_PASSWORD, ...) are hidden in every response — the start/stop/delete previews
        # carry the whole Pod too, so they're blocked here. Only the pods tool with show_secrets=true fills the values back in.
        if cls == "ConnectCredential" and getattr(obj, "is_secret", False):
            out["value"] = None
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
        return obj
    return obj
