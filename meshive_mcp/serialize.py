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
from urllib.parse import urlsplit, urlunsplit

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
                                               "creation_failure_reason", "transaction_id"},
                                       # A task's script and requirements were written by workspace members and can
                                       # hold secrets: only the tasks tool with include_code shows them.
                                       "Task": {"script", "requirements"},
                                       # A serving's failure diagnostics carry program output, and a replica's pod
                                       # name and log id are how the platform finds it, not something to show: only
                                       # the servings detail adds the failures and live numbers back, so the scale,
                                       # pause and delete previews (which carry the whole Serving) carry none.
                                       "Serving": {"failure"},
                                       "ServingReplica": {"failure", "metrics", "pod_name", "log_replica_id",
                                                          "setup_step", "step", "step_status"},
                                       "ServingStoppedReplica": {"pod_name"},
                                       # Whether the server masked a template env value; `value_set` says it instead.
                                       "TemplateEnv": {"masked"}}
# An add-files upload's token authorises the upload together with the user's key; upload_asset puts it only inside
# its opaque upload_id. A workspace file's statefulset name is not a pod_name; asset_files shows it as
# `source_pod_name` ("<statefulset>-0") instead.
_DROP_BY_CLASS.update({"FileUploadBatch": {"upload_token"}, "BatchUploadTarget": {"url"},
                       "WorkspaceFile": {"source_statefulset"}})
# A platform notice's revision and publish time are the console's bookkeeping, not part of what was announced.
_DROP_BY_CLASS.update({"PlatformNotice": {"revision", "publish_at"}})
# An inference key's value goes out only in the confirmed answer of create_inference_key and roll_inference_key, and
# an inference request's signed download links only in the inference_requests detail.
_DROP_BY_CLASS.update({"InferenceKeyCreated": {"inference_key"}, "InferenceRequest": {"download_urls"}})
# Prices per million tokens (the shared catalog): 4 decimals, see money.per_million.
_PER_MILLION_FIELDS_BY_CLASS: dict[str, set[str]] = {
    "SharedModel": {"price_per_1m_input_tokens", "price_per_1m_output_tokens"}}
# A write result's `replayed` is sent only when true: the server answered with its stored answer to an earlier request
# with the same operation_id, and the tool's next_step says so. In the activity log it is part of every row.
_KEEP_REPLAYED_FALSE = {"ActivityEvent"}

# Hourly rates have the same field name in every dataclass → decided by the name alone (3 decimals, like the console).
# current_price_per_hour is the rate before an edit (pod and storage update estimates).
_HOURLY_FIELDS = {"price_per_hour", "storage_rate_per_hour", "price_cap_per_hour", "current_price_per_hour",
                  # A task's hourly price split into compute and its own disk.
                  "compute_rate_per_hour", "total_rate_per_hour"}
# Hourly unit prices whose names don't say "per_hour" → checked together with the dataclass name. The console's CPU
# picker shows the per-core and per-GiB RAM rates of a line with 3 decimals, like every other hourly rate.
_HOURLY_FIELDS_BY_CLASS: dict[str, set[str]] = {"CpuLine": {"price_per_core", "ram_price_per_gb"},
                                                 # What renters pay per GPU per hour on a host's machine.
                                                 "MachinePrice": {"demand_per_hour", "spot_per_hour"}}
# What a pod's asset volumes cost per hour together (each volume's own rate is a `price_per_hour`).
_HOURLY_FIELDS_BY_CLASS.update({"PodAssetVolumes": {"total_price_per_hour"}})
# What a replica of a model costs per hour on the GPUs that fit it, and what a task pays per GPU per hour.
_HOURLY_FIELDS_BY_CLASS.update({"ModelAvailability": {"price_low", "price_high", "suggested_cap_per_hour"},
                                "TaskGpu": {"price_per_gpu_hour"}})
# Other amounts can't be told apart by name alone (e.g. DailyCost.pod is an amount, WorkspaceResources.pod a count)
# → checked together with the dataclass name. 2 decimals, like the console.
_USD_FIELDS: dict[str, set[str]] = {
    # The console's host screen shows earnings /hr with 2 decimals too.
    "Machine": {"earning_hourly", "earning_daily", "pending_payout"},
    "MachineDetail": {"earning_hourly", "earning_daily", "pending_payout"},
    "MachinePrice": {"storage_per_gb_month"},
    "Serving": {"shared_earned_this_month"},
    "WorkspaceDetail": {"weekly_avg_daily_cost"},
    "DailyCost": {"pod", "storage", "serverless", "task", "asset"},
    "Credit": {"balance", "auto_recharge_threshold", "auto_recharge_amount"},
    "CreditHistoryEntry": {"amount"},
    "Earnings": {"current_hourly", "daily", "accumulated_until_payout"},
    "Payout": {"amount"},
    "PayoutAccount": {"pending_amount", "minimum_payout"},
    "Task": {"cost_so_far", "total_cost", "refunded_amount"},
    "AssetStorage": {"price_per_gb_month", "estimated_monthly_cost"},
    "StorageEstimate": {"price_per_gb_month"},
    "TaskEstimate": {"max_cost"},
    # A workspace's Billing page (its `price_per_hour` is hourly by name), what sharing a serving's idle capacity
    # earned, and what referrals earned.
    "WorkspaceBilling": {"weekly_avg_daily_cost", "serverless_shared_this_month", "shared_credited_this_month"},
    "SharedAccrual": {"gross", "amount"},
    "SharedEarningModel": {"gross"},
    "Referral": {"total_earned", "reward_cap"},
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


_DISCORD_WEBHOOK_HOSTS = frozenset({"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"})
# The fixed part of a Slack or Discord webhook URL; everything after it is the secret.
_SECRET_WEBHOOK_PREFIXES = {"slack": "/services/", "discord": "/api/webhooks/"}


def webhook_platform(url: str | None) -> str:
    """"slack", "discord" or "generic" for a webhook URL, by the API server's rule."""
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
    except ValueError:
        return "generic"
    if host == "hooks.slack.com":
        return "slack"
    if host in _DISCORD_WEBHOOK_HOSTS and parts.path.startswith("/api/webhooks/"):
        return "discord"
    return "generic"


# Discord also takes versioned webhook URLs (/api/v10/webhooks/<id>/<token>). The API server counts them as generic,
# but their token is as much a secret, so they are masked and refused as arguments like the plain form.
_DISCORD_SECRET_PATH = re.compile(r"^/api(?:/v\d+)?/webhooks/")


def secret_webhook_prefix(url: str | None) -> str | None:
    """The fixed part of a webhook URL whose rest is a secret (Slack, Discord, versioned Discord), or None."""
    try:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if host == "hooks.slack.com":
        slack = _SECRET_WEBHOOK_PREFIXES["slack"]
        return slack if parts.path.startswith(slack) else "/"
    if host in _DISCORD_WEBHOOK_HOSTS:
        found = _DISCORD_SECRET_PATH.match(parts.path)
        if found:
            return found.group(0)
    return None


def mask_webhook_url(url: str | None) -> tuple[str, bool]:
    """A webhook URL as the agent may see it, and whether anything was hidden: the path of a Slack or Discord URL after
    its fixed prefix becomes "…", and the userinfo, query string and fragment of every URL are left out."""
    url = url or ""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return ("…", bool(url))
    if not host:
        return ("…", bool(url)) if url else ("", False)
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    path = parts.path
    prefix = secret_webhook_prefix(url)
    if prefix is not None:
        path = prefix + "…"
    masked = urlunsplit((parts.scheme, netloc, path, "", ""))
    return masked, masked != url


def to_dict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        cls = type(obj).__name__
        usd_fields = _USD_FIELDS.get(cls, frozenset())
        hourly_fields = _HOURLY_FIELDS | _HOURLY_FIELDS_BY_CLASS.get(cls, set())
        per_million_fields = _PER_MILLION_FIELDS_BY_CLASS.get(cls, frozenset())
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
            elif f.name in per_million_fields:
                out[f.name] = normalize_number(value) if isinstance(value, str) else to_dict(value)
                out[f"{f.name}_display"] = money.per_million(value)
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
        # A template's secret env value is never shown, whatever the server sent; `value_set` says one is stored.
        if cls == "TemplateEnv":
            if getattr(obj, "secret", False):
                out["value"] = None
            out["value_set"] = obj.value_set
        # A task's env as submitted: only the values that aren't secret (the server sends "***" for a secret one,
        # and `secret_env_keys` names them), in every response that carries a Task.
        # `env` is a property over the raw response (not a dataclass field) from SDK 1.11 on; older SDKs had a field.
        env = getattr(obj, "env", None) if cls == "Task" else None
        if isinstance(env, dict):
            secret = set(getattr(obj, "secret_env_keys", None) or ())
            out["env"] = {str(k): to_dict(v) for k, v in env.items() if k not in secret and v != "***"}
        # Who can reach the port: internal (inside the workspace only), locked (signed-in workspace members and the
        # port's access keys) or unlocked (anyone with the URL). The SDK works it out; it isn't a dataclass field.
        if cls == "PodEndpoint":
            out["access"] = obj.access
        # A webhook URL is shown masked everywhere (a Slack or Discord URL is itself a secret, and a query string or
        # userinfo can carry one); the SDK keeps it whole.
        if cls in ("WebhookEndpoint", "WebhookDelivery"):
            out["url"], out["url_masked"] = mask_webhook_url(obj.url)
            out["url_masked"] |= obj.raw.get("urlMasked") is True
            out["platform"] = obj.platform or webhook_platform(obj.url)
        if cls == "WebhookEndpoint":
            out["signed"] = out["platform"] == "generic"
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
