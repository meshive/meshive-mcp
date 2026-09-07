"""예외 → 모델용 지시문 번역.

MCP 에서 `isError: true` 응답은 모델이 읽고 스스로 다음 행동을 고른다. 그래서 스택트레이스나
서버 원문 대신 **무엇이 잘못됐고(code, message) 다음에 무엇을 해야 하는지(next_step)** 를
JSON 한 덩어리로 돌려준다. 재시도 여부는 반드시 문장으로 적는다 — 429 를 그냥 넘기면
모델은 즉시 재시도하고 상황을 악화시킨다.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from meshive.exceptions import (
    AuthenticationError,
    ConfigurationError,
    MeshiveAPIError,
    MeshiveError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

CONSOLE_URL = "https://console.meshive.ai"

NEXT_STEP_NO_KEY = (
    "Ask the user to create a Meshive API key in the console "
    f"({CONSOLE_URL}, workspace Settings > Secret) and put it in this MCP server's "
    "Authorization header as 'Bearer meshive_...'. Do not retry until they confirm it is set."
)
NEXT_STEP_INVALID_KEY = (
    "The configured API key was rejected (invalid, expired, revoked, or the account is inactive). "
    "Ask the user to check the key in the console and update the Authorization header. Do not retry."
)
NEXT_STEP_WRITE_SCOPE = (
    "This action needs an API key with the 'write' scope. Ask the user to issue one in the console "
    "and update the Authorization header. Do not retry with the current key."
)
NEXT_STEP_FORBIDDEN = (
    "The user's account is not allowed to do this (for example, not an admin of the workspace). "
    "Tell the user and do not retry."
)
NEXT_STEP_NOT_FOUND = (
    "Verify the identifier with the matching list tool (pods, storages, templates, ...). "
    "Do not guess identifiers."
)
NEXT_STEP_UNAVAILABLE = "Wait {retry_after}s and retry once. If it fails again, tell the user."


def tool_error(code: str, message: str, next_step: str, **extra: Any) -> ToolError:
    body = {"code": code, "message": message, "next_step": next_step, **extra}
    return ToolError(json.dumps(body, ensure_ascii=False))


def invalid_argument(message: str, **extra: Any) -> ToolError:
    return tool_error("invalid_argument", message,
                      "Fix the argument and call the tool again.", **extra)


def translate(exc: BaseException) -> ToolError:
    """SDK/네트워크 예외를 ToolError 로. 이미 ToolError 면 그대로."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ConfigurationError):
        return tool_error("no_api_key", "No Meshive API key was provided.", NEXT_STEP_NO_KEY)
    if isinstance(exc, AuthenticationError):
        return tool_error("invalid_api_key", exc.message or "Invalid API key.", NEXT_STEP_INVALID_KEY)
    if isinstance(exc, PermissionDeniedError):
        msg = exc.message or "Forbidden."
        if "scope" in msg.lower():
            return tool_error("write_scope_required", msg, NEXT_STEP_WRITE_SCOPE)
        return tool_error("forbidden", msg, NEXT_STEP_FORBIDDEN)
    if isinstance(exc, NotFoundError):
        return tool_error("not_found", exc.message or "Not found.", NEXT_STEP_NOT_FOUND)
    if isinstance(exc, RateLimitError):
        wait = int(exc.retry_after) if exc.retry_after else 30
        return tool_error("rate_limited", exc.message or "Rate limit exceeded.",
                          f"Do not retry immediately. Wait at least {wait}s before calling any Meshive tool again.",
                          retry_after=wait)
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
    # 그 외는 서버 버그로 간주 — SDK 가 UnexpectedToolError 로 감싸고 traceback 을 로그에 남긴다.
    raise exc
