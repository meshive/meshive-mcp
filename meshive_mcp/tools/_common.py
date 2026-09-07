"""도구 모듈 공용: 등록 데코레이터, 어노테이션, 예외 번역 래퍼."""
from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from ..errors import translate

T = TypeVar("T")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def meshive_tool(server: MCPServer, name: str, *, annotations: ToolAnnotations):
    """`server.tool` 위에 한 겹: 도구가 던진 ToolError 를 **접두어 없는 순수 JSON** 본문의
    `isError` 결과로 바꾼다. SDK 기본 동작은 "Error executing tool <name>: ..." 을 앞에 붙이는데,
    계약(§0.4)은 본문이 `{code, message, next_step}` JSON 그 자체여야 한다 — 모델이 파싱해서
    `next_step` 을 따르게 하려면 접두어가 없어야 한다.
    """
    def decorator(fn: Callable[..., Awaitable[dict[str, Any]]]):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any):
            try:
                return await fn(*args, **kwargs)
            except ToolError as exc:
                return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)

        server.tool(name=name, annotations=annotations)(wrapper)
        return fn

    return decorator


async def call(fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """SDK 호출 한 번을 감싸 예외를 모델용 ToolError 로 바꾼다."""
    try:
        return await fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — translate 가 모르는 예외는 다시 던진다
        raise translate(exc) from exc
