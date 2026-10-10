"""Exceptions → instructions for the model.

In MCP, the model reads an `isError: true` response and picks its next action itself. So instead of a stack trace
or the raw server text, it gets one JSON object saying **what went wrong (code, message) and what to do next (next_step)**.
Always say in words whether to retry — if a 429 is passed through as-is,
the model retries immediately and makes things worse.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from meshive.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    InsufficientCreditError,
    MeshiveAPIError,
    MeshiveError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

CONSOLE_URL = "https://console.meshive.ai"

KEYS_PAGE = f"{CONSOLE_URL}, workspace Settings > API keys"
# How to swap keys — keys expire (write keys default to 30 days), and Claude Code's `mcp add` rejects a name that already exists.
REPLACE_KEY = ("replace the old key in this MCP server's configuration (in Claude Code, run "
               "`claude mcp remove meshive` first, then add it again)")

NEXT_STEP_NO_KEY = (
    f"Ask the user to create a Meshive API key in the console ({KEYS_PAGE}) and put it in this MCP server's "
    "Authorization header as 'Bearer meshive_...'. Do not retry until they confirm it is set."
)
NEXT_STEP_INVALID_KEY = (
    "The configured API key was rejected (invalid, expired, revoked, or the account is inactive). "
    f"Ask the user to check the key, or create a new one in the console ({KEYS_PAGE}) and {REPLACE_KEY}. "
    "Do not retry."
)
NEXT_STEP_WRITE_SCOPE = (
    "This action needs an API key with the 'write' scope. Ask the user to create a Read & write key in the console "
    f"({KEYS_PAGE}) and {REPLACE_KEY}. Do not retry with the current key."
)
NEXT_STEP_FORBIDDEN = (
    "The user's account is not allowed to do this (for example, not an admin of the workspace). "
    "Tell the user and do not retry."
)
NEXT_STEP_NOT_FOUND = (
    "Verify the identifier with the matching list tool (pods, storages, templates, ...). "
    "Do not guess identifiers."
)
NEXT_STEP_UNAVAILABLE = ("Wait {retry_after}s. For a read, retry once. For a write, check operation_status first; "
                        "reuse the original operation_id and arguments if retrying. Never create a new operation for an unknown outcome.")


def tool_error(code: str, message: str, next_step: str, **extra: Any) -> ToolError:
    body = {"code": code, "message": message, "next_step": next_step, **extra}
    return ToolError(json.dumps(body, ensure_ascii=False))


def invalid_argument(message: str, **extra: Any) -> ToolError:
    return tool_error("invalid_argument", message,
                      "Fix the argument and call the tool again.", **extra)


# The kind of 409 is told apart by the server's detail.title. Extra info (availability, pricePerHourUsd,
# linkedPods, available) is carried in detail as-is and passed on to the model.
_CONFLICT_CODES = {
    "operation outcome unknown": ("operation_outcome_unknown", "Check operation_status and the recorded task/transaction. "
                                   "The request will not execute again; pending/unknown records require reconciliation."),
    "price unavailable": ("price_unavailable", "The server cannot quote this capped request. Keep the user's cap; "
                           "do not remove or increase it without their explicit consent."),
    "data loss consent required": ("data_loss_consent_required", "Show the permanent workspace-file loss warning for "
                                    "this pod and placement. Obtain separate consent before setting allow_data_loss=true."),
    "no capacity": ("no_capacity", "Nothing is available for this request right now. Show the `available`/`availability` "
                                   "details to the user and suggest a smaller request, a different GPU, or trying later. "
                                   "Do not retry blindly."),
    # A 409 after retrying a create whose response was lost is common — only saying "use another name" makes the model create one more resource.
    "name taken": ("name_taken", "A resource with this name already exists. If you just tried to create it and did not "
                                 "get a clear answer, that is probably it — check the matching list tool before creating "
                                 "anything else. Otherwise pick another name or use the existing one."),
    "price exceeds cap": ("price_exceeds_cap", "The estimate is above the user's price cap. Show `pricePerHourUsd` and ask "
                                               "whether to raise the cap or choose cheaper hardware."),
    "storage in use": ("storage_in_use", "The storage is mounted by the pods in `linkedPods`. Stop/delete or detach them "
                                         "first (ask the user)."),
    "request in progress": ("in_progress", "The same request is still being processed. Wait a few seconds, then check the "
                                           "matching list tool instead of resubmitting."),
    "vram tier required": ("vram_tier_required", "Pass `gpu_vram_gb` — this GPU model comes in several VRAM tiers "
                                                 "(see `available`)."),
}


def _conflict(exc: ConflictError) -> ToolError:
    title = (exc.title or "").strip().lower()
    code, next_step = _CONFLICT_CODES.get(title, ("conflict", "Check the current state with the matching list tool "
                                                              "before retrying."))
    detail = exc.raw.get("detail") if isinstance(exc.raw, dict) else None
    extra = {k: v for k, v in (detail or {}).items() if k not in ("title", "message")} if isinstance(detail, dict) else {}
    return tool_error(code, exc.message or exc.title or "Conflict.", next_step, **extra)


def translate(exc: BaseException) -> ToolError:
    """SDK/network exceptions → ToolError. A ToolError passes through unchanged."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ConfigurationError):
        return tool_error("no_api_key", "No Meshive API key was provided.", NEXT_STEP_NO_KEY)
    if isinstance(exc, AuthenticationError):
        return tool_error("invalid_api_key", exc.message or "Invalid API key.", NEXT_STEP_INVALID_KEY)
    if isinstance(exc, PermissionDeniedError):
        msg = exc.message or "Forbidden."
        low = msg.lower()
        if "scope" in low:
            return tool_error("write_scope_required", msg, NEXT_STEP_WRITE_SCOPE)
        if "member of workspace" in low or "not a member" in low:
            # The backend answers a nonexistent or someone else's workspace id with 403 — for the model that's a "wrong id".
            return tool_error("unknown_workspace", msg,
                              "The workspace id is wrong or not the user's. Call the workspaces tool and use "
                              "the `namespace_name` value (not the label).")
        return tool_error("forbidden", msg, NEXT_STEP_FORBIDDEN)
    if isinstance(exc, NotFoundError):
        if exc.title == "Pod Creation Failed":
            # The pod is gone because creating it failed, not because of a wrong id — re-listing won't find it.
            return tool_error("pod_creation_failed", exc.message or "Pod creation failed.",
                              "Tell the user the reason. Don't retry this pod; fix the cause (template, command, "
                              "inputs) and create a new pod.")
        return tool_error("not_found", exc.message or "Not found.", NEXT_STEP_NOT_FOUND)
    if isinstance(exc, RateLimitError):
        wait = int(exc.retry_after) if exc.retry_after else 30
        return tool_error("rate_limited", exc.message or "Rate limit exceeded.",
                          f"Do not retry immediately. Wait at least {wait}s before calling any Meshive tool again.",
                          retry_after=wait)
    if isinstance(exc, InsufficientCreditError):
        return tool_error("insufficient_credit", exc.message or "Insufficient credit.",
                          f"Ask the user to top up credit at {CONSOLE_URL} before retrying. Do not retry on your own.")
    if isinstance(exc, ConflictError):
        return _conflict(exc)
    if isinstance(exc, MeshiveAPIError):
        status = exc.status_code
        msg = exc.message or f"HTTP {status}"
        if status == 402:
            return tool_error("insufficient_credit", msg,
                              f"Ask the user to top up credit at {CONSOLE_URL} before retrying.")
        if status == 409:
            return tool_error("conflict", msg,
                              "Check the current state with the matching list tool before retrying.")
        if status in (400, 422):
            return tool_error("invalid_request", msg,
                              "The server rejected the request. Fix the arguments as described and retry once.")
        if status == 503:
            return tool_error("temporarily_unavailable", msg, NEXT_STEP_UNAVAILABLE.format(retry_after=30),
                              retry_after=30)
        if status >= 500:
            return tool_error("server_error", msg, NEXT_STEP_UNAVAILABLE.format(retry_after=15),
                              retry_after=15)
        return tool_error("api_error", msg, "Tell the user what the server said. Do not retry blindly.",
                          status=status)
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError)):
        return tool_error("temporarily_unavailable", f"Could not reach the Meshive API: {exc.__class__.__name__}",
                          NEXT_STEP_UNAVAILABLE.format(retry_after=15), retry_after=15)
    if isinstance(exc, MeshiveError):
        return tool_error("sdk_error", str(exc), "Tell the user. Do not retry.")
    # Anything else is treated as a server bug — the SDK wraps it in UnexpectedToolError and logs the traceback.
    raise exc
