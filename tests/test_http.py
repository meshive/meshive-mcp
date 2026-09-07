"""HTTP transport: /healthz, Bearer 헤더 전달, GET /mcp 거절."""
import json

import httpx
import pytest

pytestmark = pytest.mark.anyio

from meshive_mcp.server import create_http_app, create_server

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(method, params=None, id=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id is not None:
        body["id"] = id
    return body


@pytest.fixture
async def http():
    app = create_http_app(create_server())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as c:
        # lifespan 을 직접 돌린다 — ASGITransport 는 startup 이벤트를 보내지 않는다.
        async with app.router.lifespan_context(app):
            yield c


async def test_healthz(http):
    r = await http.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_get_mcp_is_not_a_stream(http):
    r = await http.get("/mcp", headers={"Accept": "text/event-stream"})
    assert r.status_code == 405


async def test_bearer_reaches_sdk_client(http, fake):
    key = "meshive_" + "b" * 64
    init = await http.post("/mcp", headers={**HEADERS, "Authorization": f"Bearer {key}"},
                           json=_rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                    "clientInfo": {"name": "test-agent", "version": "9.9"}}))
    assert init.status_code == 200, init.text
    r = await http.post("/mcp", headers={**HEADERS, "Authorization": f"Bearer {key}", "User-Agent": "codex/1.0"},
                        json=_rpc("tools/call", {"name": "account", "arguments": {}}, id=2))
    assert r.status_code == 200, r.text
    payload = r.json()["result"]
    assert not payload.get("isError"), payload
    body = json.loads(payload["content"][0]["text"])
    assert body["user"]["email"] == "u@example.com"
    assert fake["last"].key == key
    # 무상태라 clientInfo 가 이 요청에 없다 → User-Agent 폴백
    assert fake["last"].label in ("test-agent/9.9", "codex/1.0")


async def test_missing_bearer_is_tool_error_not_http_401(http, fake):
    r = await http.post("/mcp", headers=HEADERS, json=_rpc("tools/call", {"name": "account", "arguments": {}}))
    assert r.status_code == 200
    payload = r.json()["result"]
    assert payload["isError"] and json.loads(payload["content"][0]["text"])["code"] == "no_api_key"
