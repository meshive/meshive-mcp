"""요청마다 SDK 클라이언트를 만든다.

무상태 서버라 커넥션 풀을 요청 간에 공유하지 않는다(키가 다르므로 공유하면 안 된다).
`AsyncMeshive(api_key=None)` 은 환경변수·credentials 파일로 폴백하기 때문에, 키가 없으면
클라이언트를 만들지 않고 바로 no_api_key 를 낸다 — 공용 서버가 운영자의 키로 남의 요청을
처리하는 사고를 원천 차단.
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
        # SDK 0.0.x 에 커스텀 헤더 옵션이 없어 httpx 기본 헤더에 얹는다(요청 헤더와 병합됨).
        # 0.0.8 에서 `headers=` 인자가 생기면 이 줄을 교체한다.
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
