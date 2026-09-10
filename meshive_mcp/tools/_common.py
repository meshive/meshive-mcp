"""도구 모듈 공용: 등록 데코레이터, 어노테이션, 예외 번역 래퍼."""
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
_operation: ContextVar[dict | None] = ContextVar("mcp_operation", default=None)
_WRITES = {"create_pod", "stop_pod", "start_pod", "restart_pod", "delete_pod", "create_storage",
           "delete_storage", "deploy_serving", "scale_serving", "pause_serving", "delete_serving",
           "submit_task", "stop_task"}


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
# 생성/배포/제출: 같은 호출을 반복하면 자원이 늘어난다(서버 Idempotency-Key 는 SDK 재시도용).
CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
# 정지/시작/재시작/스케일/일시정지: 반복해도 같은 상태로 수렴.
MUTATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

CONFIRM_STEP = ("Show this to the user and ask for an explicit go-ahead. Only then call this tool again with the same "
                "arguments and confirm=true. Never set confirm=true on your own.")


def preview(kind: str, payload: dict[str, Any], question: str) -> dict[str, Any]:
    """confirm=false 응답: 아무것도 만들지/지우지 않았음을 명시하고 다음 행동을 지시한다."""
    return {"confirmed": False, "action": kind, **payload, "question": question, "next_step": CONFIRM_STEP}


def meshive_tool(server: MCPServer, name: str, *, annotations: ToolAnnotations):
    """`server.tool` 위에 한 겹: (1) 정상 결과를 compact JSON 텍스트 + structured_content 로,
    (2) 도구가 던진 ToolError 를 **접두어 없는 순수 JSON** 본문의 `isError` 결과로 바꾼다. SDK 기본 동작은 "Error executing tool <name>: ..." 을 앞에 붙이는데,
    계약(§0.4)은 본문이 `{code, message, next_step}` JSON 그 자체여야 한다 — 모델이 파싱해서
    `next_step` 을 따르게 하려면 접두어가 없어야 한다.
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
        server.tool(name=name, annotations=annotations)(wrapper)
        return fn

    return decorator


async def call(fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """SDK 호출 한 번을 감싸 예외를 모델용 ToolError 로 바꾼다."""
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
    except Exception as exc:  # noqa: BLE001 — translate 가 모르는 예외는 다시 던진다
        operation = _operation.get()
        if is_write and operation is not None and getattr(exc, "operation_method", None):
            operation["lookup"] = {"method": exc.operation_method, "path": exc.operation_path}
        raise translate(exc) from exc
