"""Build an SDK client per request.

The server is stateless, so connection pools aren't shared across requests (keys differ, so they must not be).
`AsyncMeshive(api_key=None)` falls back to environment variables and the credentials file, so without a key
no client is built and no_api_key is returned right away — this rules out a shared server handling
someone else's request with the operator's key.
"""
from __future__ import annotations

from typing import Any, AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import Context
from meshive import AsyncMeshive

from . import __version__
from .auth import api_key, client_label
from .errors import NEXT_STEP_NO_KEY, tool_error
from .settings import settings

CLIENT_HEADER = "X-Meshive-Client"


def _new_client(key: str, label: str) -> AsyncMeshive:
    client = AsyncMeshive(key, base_url=settings.base_url, timeout=settings.request_timeout,
                          max_retries=1)
    http = getattr(client, "_client", None)
    if http is not None:
        # SDK 0.0.x has no custom header option, so it goes on httpx's default headers (merged with request headers).
        # Replace this line once 0.0.8 adds the `headers=` argument.
        http.headers[CLIENT_HEADER] = label
        http.headers["User-Agent"] = f"meshive-mcp/{__version__} meshive-python/{_sdk_version()}"
    return client


def _sdk_version() -> str:
    try:
        from meshive import __version__ as v
        return v
    except Exception:  # noqa: BLE001
        return "unknown"


@asynccontextmanager
async def meshive_client(ctx: Context[Any, Any]) -> AsyncIterator[AsyncMeshive]:
    key = api_key(ctx)
    if not key:
        raise tool_error("no_api_key", "No Meshive API key was provided with this request.", NEXT_STEP_NO_KEY)
    client = _new_client(key, client_label(ctx))
    try:
        yield client
    finally:
        await client.close()
