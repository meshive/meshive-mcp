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
#
# One-time SSH logins never go out through to_dict: ssh_access and connect_storage put the command and password into their
# own response, once, and the browser-terminal URL (which carries the password) is never sent. Machine logins and
# install commands likewise go out only in the confirmed answer of machine_credentials and host_install_command, and
# the value of a port access key only in the confirmed answer of create_access_key and roll_access_key.
_DROP_BY_CLASS: dict[str, set[str]] = {"Credit": {"paid_balance", "bonus_balance"},
                                       "SshAccess": {"password", "web_url"},
                                       "StorageConnection": {"ssh"},
                                       "SshCredentials": {"password"},
                                       "IpmiCredentials": {"password"},
                                       "GrafanaCredentials": {"password"},
                                       "InstallCommand": {"command"},
                                       "AccessKeyCreated": {"access_key"},
                                       # A pod's exits and creation failure carry text from the pod (`message`,
                                       # `output`, and the old `creation_failure_reason` that quotes it); only the pods
                                       # tool shows them (the instructions mark those fields untrusted), never a write
                                       # preview that serializes the whole Pod. The restart count goes with the last
                                       # exit and the create transaction with a pod that has no record yet, so items
                                       # carry none of them as a null.
                                       "Pod": {"recent_exits", "last_exit", "restart_count", "creation_failure",
                                               "creation_failure_reason", "transaction_id"}}
# A write result's `replayed` is sent only when true: the server answered with its stored answer to an earlier request
# with the same operation_id, and the tool's next_step says so. In the activity log it is part of every row.
_KEEP_REPLAYED_FALSE = {"ActivityEvent"}

# Hourly rates have the same field name in every dataclass → decided by the name alone (3 decimals, like the console).
# current_price_per_hour is the rate before an edit (pod and storage update estimates).
_HOURLY_FIELDS = {"price_per_hour", "storage_rate_per_hour", "price_cap_per_hour", "current_price_per_hour"}
# Hourly unit prices whose names don't say "per_hour" → checked together with the dataclass name. The console's CPU
# picker shows the per-core and per-GiB RAM rates of a line with 3 decimals, like every other hourly rate.
_HOURLY_FIELDS_BY_CLASS: dict[str, set[str]] = {"CpuLine": {"price_per_core", "ram_price_per_gb"}}
# Other amounts can't be told apart by name alone (e.g. DailyCost.pod is an amount, WorkspaceResources.pod a count)
# → checked together with the dataclass name. 2 decimals, like the console.
_USD_FIELDS: dict[str, set[str]] = {
    "Machine": {"earning_hourly"},                    # the console's host screen shows earnings /hr with 2 decimals too
    "MachineDetail": {"earning_hourly"},
    "WorkspaceDetail": {"weekly_avg_daily_cost"},
    "DailyCost": {"pod", "storage", "serverless", "task", "asset"},
    "Credit": {"balance", "auto_recharge_threshold", "auto_recharge_amount"},
    "CreditHistoryEntry": {"amount"},
    "Earnings": {"current_hourly", "daily", "accumulated_until_payout"},
    "Payout": {"amount"},
    "PayoutAccount": {"pending_amount", "minimum_payout"},
    "Task": {"cost_so_far", "total_cost"},
    "AssetStorage": {"price_per_gb_month", "estimated_monthly_cost"},
    "StorageEstimate": {"price_per_gb_month"},
    "TaskEstimate": {"max_cost"},
}
# A pod estimate breakdown is a set of hourly amounts like {"gpu": "0.068", ...} — the console's Receipt shows 3 decimals per line too.
_HOURLY_DICT_FIELDS = {("PodEstimate", "breakdown"), ("PodUpdateEstimate", "breakdown")}
# Fields holding a dict, or a list of dicts, with an hourly `price_per_hour` of their own: the scratch disks of a pod and
# of a pod estimate.
_HOURLY_ITEM_FIELDS = {("Pod", "scratch_disks"), ("PodEstimate", "scratch_disk"), ("PodUpdateEstimate", "scratch_disk")}
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


def _with_hourly_display(value: Any) -> Any:
    """Adds `price_per_hour_display` to a dict (or to each dict of a list) that has a `price_per_hour`."""
    if isinstance(value, list):
        return [_with_hourly_display(v) for v in value]
    if isinstance(value, dict) and "price_per_hour" in value:
        raw = value["price_per_hour"]
        value = {**value, "price_per_hour": normalize_number(raw) if isinstance(raw, str) else raw,
                 "price_per_hour_display": money.hourly(raw)}
    return value


def _estimate_scratch_disk(disk: Any, obj: Any) -> Any:
    """A pod estimate's scratch disk. Its `price_per_hour` covers only the disks the request creates, so it is left
    out when none is new, like `breakdown` leaves it out: a disk the pod keeps isn't free. `total_price_per_hour` is
    what every disk costs per hour after the request, from servers that send it."""
    if not isinstance(disk, dict):
        return disk
    disk = dict(disk)
    total = disk.pop("total_price_per_hour", None)
    if total is None:                       # an SDK that doesn't read it yet
        body = getattr(obj, "raw", None)
        raw = body.get("scratchDisk") if isinstance(body, dict) else None
        total = raw.get("totalPricePerHourUsd") if isinstance(raw, dict) else None
    if not disk.get("new_paths"):
        disk.pop("price_per_hour", None)
        disk.pop("price_per_hour_display", None)
    if total is not None:
        disk["total_price_per_hour"] = normalize_number(total) if isinstance(total, str) else total
        disk["total_price_per_hour_display"] = money.hourly(total)
    return disk


def to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        cls = type(obj).__name__
        usd_fields = _USD_FIELDS.get(cls, frozenset())
        hourly_fields = _HOURLY_FIELDS | _HOURLY_FIELDS_BY_CLASS.get(cls, set())
        drop = _DROP_BY_CLASS.get(cls, frozenset())
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            if f.name in _DROP or f.name in drop:
                continue
            value = getattr(obj, f.name)
            if f.name == "replayed" and value is not True and cls not in _KEEP_REPLAYED_FALSE:
                continue
            out[f.name] = to_dict(value)
            if f.name in hourly_fields:
                out[f.name] = normalize_number(value) if isinstance(value, str) else to_dict(value)
                out[f"{f.name}_display"] = money.hourly(value)
            elif f.name in usd_fields:
                out[f.name] = normalize_number(value) if isinstance(value, str) else to_dict(value)
                out[f"{f.name}_display"] = money.usd(value)
            elif (cls, f.name) in _HOURLY_DICT_FIELDS and isinstance(value, dict):
                out[f.name] = {str(k): normalize_number(v) if isinstance(v, str) else to_dict(v)
                               for k, v in value.items()}
                out[f"{f.name}_display"] = {str(k): money.hourly(v) for k, v in value.items()}
            elif (cls, f.name) in _HOURLY_ITEM_FIELDS:
                out[f.name] = _with_hourly_display(out[f.name])
                if f.name == "scratch_disk":
                    out[f.name] = _estimate_scratch_disk(out[f.name], obj)
        # Secret connect values (ComfyUI's ACCESS_PASSWORD, ...) are hidden in every response — the start/stop/delete previews
        # carry the whole Pod too, so they're blocked here. Only the pods tool with show_secrets=true fills the values back in.
        if cls == "ConnectCredential" and getattr(obj, "is_secret", False):
            out["value"] = None
        # Who can reach the port: internal (inside the workspace only), locked (signed-in workspace members and the
        # port's access keys) or unlocked (anyone with the URL). The SDK works it out; it isn't a dataclass field.
        if cls == "PodEndpoint":
            out["access"] = obj.access
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
