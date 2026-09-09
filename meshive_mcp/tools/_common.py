"""도구 모듈 공용: 등록 데코레이터, 어노테이션, 예외 번역 래퍼."""
from __future__ import annotations

import functools
import json
from typing import Any, Awaitable, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from ..errors import translate

T = TypeVar("T")

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
            try:
                result = await fn(*args, **kwargs)
            except ToolError as exc:
                return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)
            # SDK 기본 직렬화는 indent=2 라 목록 응답이 부풀어 오른다(토큰). compact 로 직접 만든다.
            text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=result)

        server.tool(name=name, annotations=annotations)(wrapper)
        return fn

    return decorator


async def call(fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """SDK 호출 한 번을 감싸 예외를 모델용 ToolError 로 바꾼다."""
    try:
        return await fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — translate 가 모르는 예외는 다시 던진다
        raise translate(exc) from exc
