"""Request → API key / client identification.

Authentication passes the HTTP header straight through. The server neither stores nor validates keys —
when the API returns 401/403, errors.translate turns it into a sentence for the model.
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
    # Starlette header keys are lowercase. Check both in case of other transports.
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
    """`<client name>/<version>` — MCP clientInfo first, otherwise the User-Agent.

    Over stateless HTTP, initialize may not come with every request, so clientInfo can be empty.
    Then the HTTP User-Agent (e.g. claude-code/1.x) is the only clue.
    """
    try:
        params = ctx.connection.client_params
        info = getattr(params, "client_info", None) if params else None
        if info and getattr(info, "name", None):
            version = getattr(info, "version", "") or ""
            return f"{info.name}/{version}".rstrip("/")
    except Exception:  # noqa: BLE001 — identification is best-effort and never blocks a tool call
        pass
    headers = ctx.headers or {}
    ua = headers.get("user-agent") or headers.get("User-Agent")
    return ua or "unknown"
