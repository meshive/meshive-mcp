"""Shared by the tool modules: registration decorator, annotations, exception translation wrapper."""
from __future__ import annotations

import functools
import inspect
import json
import re
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .. import money
from ..errors import translate, tool_error

T = TypeVar("T")
MAX_RESPONSE_BYTES = 1024 * 1024
# Errors whose next_step already says whether and how to call again, so the generic "retry with the same operation_id and
# the same arguments" advice would contradict it: a failed create is final, a start refused for data-loss consent is
# called again with the placement it names and allow_data_loss=true once the user agrees, an edit refused for a
# move, local storage or watched folders is called again with the argument the user agreed to, a taken name is
# checked against the list or changed, and a refused workspace delete is checked again without confirm or left alone.
# Host refusals are fixed and asked about again (new times, a recipient from the machine's clients, the reason the
# machine can't be listed, the window's state), waited out (a port check), or not retried (a removal in progress, a
# login the machine doesn't have).
_OWN_RETRY_ADVICE_CODES = {"name_taken", "pod_creation_failed", "data_loss_consent_required", "node_migration_required",
                           "cannot_change_resources", "harvest_roots_orphaned", "model_set_unavailable",
                           "hf_token_required", "hf_license_not_accepted", "hf_access_not_approved",
                           "hf_token_rejected", "workspace_not_empty", "workspace_not_deletable",
                           "insufficient_notice", "maintenance_overlap", "maintenance_state", "recipient_not_allowed",
                           "message_rejected", "listing_refused", "port_check_cooldown", "delete_in_progress",
                           "credentials_not_available"}
_operation: ContextVar[dict | None] = ContextVar("mcp_operation", default=None)
# SDK methods that take an Idempotency-Key: the server keeps an operation record for them, so a retry with the same
# operation_id gets the first answer back and operation_status can look the operation up.
_WRITES = {"create_pod", "stop_pod", "start_pod", "restart_pod", "delete_pod", "update_pod", "create_storage",
           "update_storage", "delete_storage", "deploy_serving", "scale_serving", "pause_serving", "delete_serving",
           "submit_task", "stop_task", "register_model", "delete_model", "import_asset", "set_watched_folders",
           "create_workspace", "update_workspace", "delete_workspace",
           "schedule_maintenance", "update_maintenance", "cancel_maintenance", "end_maintenance",
           "extend_maintenance", "set_machine_listing", "retry_machine_setup", "delete_machine",
           "send_host_message", "mark_host_messages_read"}
# SDK methods that change something but are sent without an operation_id, so the server keeps no operation record and
# a repeated call runs again. connect_storage, ssh_access, reveal_machine_credentials and issue_install_command are
# left out of _WRITES on purpose: their responses carry a password or an install command, which the server must not
# keep as a replayable record. retry_port_check has no operation record either; its cooldown keeps it from repeating.
_UNRECORDED = {"connect_storage", "ssh_access", "reveal_machine_credentials", "issue_install_command",
               "retry_port_check"}


def _bounded_result(result: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    response = CallToolResult(content=[TextContent(type="text", text=text)],
                              structured_content=result, is_error=is_error)
    # Bound the final envelope, including the duplicated structured/text payload.
    if len(response.model_dump_json(by_alias=True).encode("utf-8")) > MAX_RESPONSE_BYTES:
        body = {"code": "response_too_large", "message": "The response exceeds 1 MiB.",
                "next_step": "Request fewer items or log lines. Check the resource state before retrying a write."}
        if result.get("operation_id"):
            body["operation_id"] = result["operation_id"]
        if result.get("operation_lookup"):
            body["operation_lookup"] = result["operation_lookup"]
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(body))], is_error=True)
    return response

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
# Create/deploy/submit: repeating the same call adds resources (the server's Idempotency-Key is for SDK retries).
CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
# Stop/start/restart/scale/pause: repeating converges to the same state.
MUTATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)
# Reveal a login or issue an install command: every call reveals or issues again and emails the account owner again.
REVEAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

CONFIRM_STEP = ("Show this to the user and ask for an explicit go-ahead. Only then call this tool again with the same "
                "arguments and confirm=true. Never set confirm=true on your own.")


def preview(kind: str, payload: dict[str, Any], question: str) -> dict[str, Any]:
    """confirm=false response: states that nothing was created or deleted and tells the model what to do next."""
    return {"confirmed": False, "action": kind, **payload, "question": question, "next_step": CONFIRM_STEP}


def price_change(est: Any) -> str:
    """Sentence with the hourly price before and after an edit, from a pod or storage update estimate."""
    after = money.hourly(est.price_per_hour)
    if est.current_price_per_hour is None:
        return f" Its price will be {after}/hour."
    before = money.hourly(est.current_price_per_hour)
    return (f" Its price stays at {after}/hour." if before == after else
            f" Its price goes from {before}/hour to {after}/hour.")


_RECORDED_NOTE = ("\nEvery write requires operation_id. Reuse the ID returned by the preview, or generate a UUID "
                  "before the first call. Keep it for confirmation and all retries; a new ID means a new operation. "
                  "After a timeout, check operation_status and resource state before retrying.")
_UNRECORDED_NOTE = ("\nThe server keeps no operation record of this call, so operation_status can't look it up and a "
                    "repeated {}call runs again.")
_RECORDED_RETRY = (" Check the resource/operation state first. Any retry MUST reuse this operation_id and the same "
                   "arguments; never submit a new operation for an unknown outcome.")
_UNRECORDED_RETRY = (" The server keeps no operation record of this call, so a retry runs it again: retry only if the "
                     "user still wants it.")


def meshive_tool(server: MCPServer, name: str, *, title: str, annotations: ToolAnnotations,
                 operation_record: bool = True):
    """One layer over `server.tool`: (1) a normal result as compact JSON text + structured_content,
    (2) a ToolError raised by the tool as an `isError` result whose body is **pure JSON with no prefix**. The SDK's default prepends "Error executing tool <name>: ...",
    but the error body must be the `{code, message, next_step}` JSON itself — the model has to parse it
    and follow `next_step`, so there can't be a prefix.

    operation_record=False marks a write whose SDK call is in `_UNRECORDED`: its description and retry advice say
    that a repeated call runs again instead of pointing at operation_status. A tool that sends a recorded write too
    keeps the default; its failed unrecorded call still gets the unrecorded retry advice.
    """
    def decorator(fn: Callable[..., Awaitable[dict[str, Any]]]):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any):
            token = None
            op = None
            operation = {}
            try:
                if not annotations.read_only_hint:
                    supplied = inspect.signature(fn).bind(*args, **kwargs).arguments.get("operation_id")
                    op = supplied or str(uuid.uuid4())
                    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", op):
                        raise tool_error("invalid_operation_id", "operation_id must be 8-128 characters of [A-Za-z0-9._:-].",
                                         "Use the operation_id returned by the preview, or generate a UUID before the first call.")
                    operation = {"id": op, "supplied": bool(supplied)}
                    token = _operation.set(operation)
                result = await fn(*args, **kwargs)
            except ToolError as exc:
                try:
                    result = json.loads(str(exc))
                except (ValueError, TypeError):
                    result = {"code": "tool_error", "message": str(exc)}
                if op:
                    result["operation_id"] = op
                    if "lookup" in operation:
                        result["operation_lookup"] = operation["lookup"]
                    if result.get("code") not in _OWN_RETRY_ADVICE_CODES:
                        unrecorded = not operation_record or operation.get("unrecorded")
                        result["next_step"] = (result.get("next_step", "") +
                                               (_UNRECORDED_RETRY if unrecorded else _RECORDED_RETRY))
                return _bounded_result(result, is_error=True)
            finally:
                if token is not None:
                    _operation.reset(token)
            if op:
                result["operation_id"] = op
                if "lookup" in operation:
                    result["operation_lookup"] = operation["lookup"]
                if result.get("confirmed") is False:
                    result["next_step"] += (" Reuse this operation_id for confirmation and every retry."
                                            if operation_record else " Reuse this operation_id for the confirmation.")
            return _bounded_result(result)

        if not annotations.read_only_hint:
            if operation_record:
                note = _RECORDED_NOTE
            else:
                takes_confirm = "confirm" in inspect.signature(fn).parameters
                note = _UNRECORDED_NOTE.format("confirmed " if takes_confirm else "")
            wrapper.__doc__ = (wrapper.__doc__ or "") + note
        # Several tools share the common annotation constants, so the title goes only on the copy.
        server.tool(name=name, title=title, annotations=annotations.model_copy(update={"title": title}))(wrapper)
        return fn

    return decorator


async def call(fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """Wrap one SDK call and turn exceptions into a ToolError for the model."""
    name = getattr(fn, "__name__", "")
    is_write = name in _WRITES
    if name in _UNRECORDED:
        # A failure of this call gets the "a retry runs it again" advice, also in a tool whose other call is recorded.
        operation = _operation.get()
        if operation is not None:
            operation["unrecorded"] = True
    try:
        if is_write:
            operation = _operation.get()
            if operation is None or not operation["supplied"]:
                raise tool_error("operation_id_required", "No write was sent. A stable operation_id is required.",
                                 "Use the preview's operation_id or generate a UUID, then reuse it for every retry.")
            kwargs["idempotency_key"] = operation["id"]
        result = await fn(*args, **kwargs)
        raw = getattr(result, "raw", {})
        operation = _operation.get()
        if is_write and operation is not None and raw.get("operationMethod"):
            operation["lookup"] = {"method": raw["operationMethod"], "path": raw["operationPath"]}
        return result
    except Exception as exc:  # noqa: BLE001 — exceptions translate doesn't know are re-raised
        operation = _operation.get()
        if is_write and operation is not None and getattr(exc, "operation_method", None):
            operation["lookup"] = {"method": exc.operation_method, "path": exc.operation_path}
        raise translate(exc) from exc
