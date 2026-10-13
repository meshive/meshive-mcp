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
    # Passed through the SDK's `headers=` so they land on every request. The SDK sets its own User-Agent per
    # request, which would override anything put on the httpx client's default headers — so the API would see
    # the bare SDK instead of this server.
    return AsyncMeshive(key, base_url=settings.base_url, timeout=settings.request_timeout, max_retries=1,
                        headers={CLIENT_HEADER: label, "User-Agent": user_agent()})


def user_agent() -> str:
    """What this server sends as User-Agent to the Meshive API: its own version, then the SDK's."""
    return f"meshive-mcp/{__version__} meshive-python/{_sdk_version()}"


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
