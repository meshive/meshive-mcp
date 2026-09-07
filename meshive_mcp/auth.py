"""요청 → API 키 / 클라이언트 식별.

인증은 HTTP 헤더를 그대로 통과시키는 방식이다. 서버는 키를 저장하지도 검증하지도 않는다 —
WSB 가 401/403 을 주면 errors.translate 가 모델용 문장으로 바꾼다.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from mcp.server.mcpserver import Context

from .settings import settings

API_KEY_PREFIX = "meshive_"


def api_key_from_headers(headers: Mapping[str, str] | None) -> str | None:
    if not headers:
        return None
    # Starlette 헤더는 소문자 키. 다른 transport 대비 양쪽 다 본다.
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[len("bearer "):].strip()
    return token if token.startswith(API_KEY_PREFIX) else None


def api_key(ctx: Context[Any, Any]) -> str | None:
    key = api_key_from_headers(ctx.headers)
    if key:
        return key
    if settings.env_api_key_fallback:
        return os.getenv("MESHIVE_API_KEY") or None
    return None


def client_label(ctx: Context[Any, Any]) -> str:
    """`<client name>/<version>` — MCP clientInfo 우선, 없으면 User-Agent.

    무상태 HTTP 에서는 initialize 가 매 요청에 없을 수 있어 clientInfo 가 비기도 한다.
    그때는 HTTP User-Agent(예: claude-code/1.x)가 유일한 단서다.
    """
    try:
        params = ctx.connection.client_params
        info = getattr(params, "client_info", None) if params else None
        if info and getattr(info, "name", None):
            version = getattr(info, "version", "") or ""
            return f"{info.name}/{version}".rstrip("/")
    except Exception:  # noqa: BLE001 — 식별은 best-effort, 도구 호출을 막지 않는다
        pass
    headers = ctx.headers or {}
    ua = headers.get("user-agent") or headers.get("User-Agent")
    return ua or "unknown"
