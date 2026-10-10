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

from ..errors import translate, tool_error

T = TypeVar("T")
MAX_RESPONSE_BYTES = 1024 * 1024
# Errors that end the operation for good — the generic "retry with the same operation_id" advice would contradict them.
_NO_RETRY_CODES = {"pod_creation_failed"}
_operation: ContextVar[dict | None] = ContextVar("mcp_operation", default=None)
_WRITES = {"create_pod", "stop_pod", "start_pod", "restart_pod", "delete_pod", "create_storage",
           "delete_storage", "deploy_serving", "scale_serving", "pause_serving", "delete_serving",
           "submit_task", "stop_task", "register_model", "delete_model", "import_asset", "set_watched_folders"}


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

CONFIRM_STEP = ("Show this to the user and ask for an explicit go-ahead. Only then call this tool again with the same "
                "arguments and confirm=true. Never set confirm=true on your own.")


def preview(kind: str, payload: dict[str, Any], question: str) -> dict[str, Any]:
    """confirm=false response: states that nothing was created or deleted and tells the model what to do next."""
    return {"confirmed": False, "action": kind, **payload, "question": question, "next_step": CONFIRM_STEP}


def meshive_tool(server: MCPServer, name: str, *, title: str, annotations: ToolAnnotations):
    """One layer over `server.tool`: (1) a normal result as compact JSON text + structured_content,
    (2) a ToolError raised by the tool as an `isError` result whose body is **pure JSON with no prefix**. The SDK's default prepends "Error executing tool <name>: ...",
    but the error body must be the `{code, message, next_step}` JSON itself — the model has to parse it
    and follow `next_step`, so there can't be a prefix.
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
                    if result.get("code") not in _NO_RETRY_CODES:
                        result["next_step"] = (result.get("next_step", "") +
                            " Check the resource/operation state first. Any retry MUST reuse this operation_id and the same arguments; never submit a new operation for an unknown outcome.")
                return _bounded_result(result, is_error=True)
            finally:
                if token is not None:
                    _operation.reset(token)
            if op:
                result["operation_id"] = op
                if "lookup" in operation:
                    result["operation_lookup"] = operation["lookup"]
                if result.get("confirmed") is False:
                    result["next_step"] += " Reuse this operation_id for confirmation and every retry."
            return _bounded_result(result)

        if not annotations.read_only_hint:
            wrapper.__doc__ = (wrapper.__doc__ or "") + (
                "\nEvery write requires operation_id. Reuse the ID returned by the preview, or generate a UUID before "
                "the first call. Keep it for confirmation and all retries; a new ID means a new operation. "
                "After a timeout, check operation_status and resource state before retrying.")
        # Several tools share the common annotation constants, so the title goes only on the copy.
        server.tool(name=name, title=title, annotations=annotations.model_copy(update={"title": title}))(wrapper)
        return fn

    return decorator


async def call(fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """Wrap one SDK call and turn exceptions into a ToolError for the model."""
    is_write = getattr(fn, "__name__", "") in _WRITES
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
