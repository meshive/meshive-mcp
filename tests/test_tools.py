"""도구를 in-process MCP 클라이언트로 끝까지 호출한다 (스키마 → 인자 검증 → 실행 → 결과 직렬화)."""
import json

import pytest

pytestmark = pytest.mark.anyio
from meshive.exceptions import AuthenticationError, RateLimitError

READ_TOOLS = {"account", "workspaces", "pods", "storages", "gpus", "templates", "servings", "tasks",
              "assets", "machines", "billing_history"}


def _payload(result):
    assert result.content and result.content[0].type == "text"
    return json.loads(result.content[0].text)


async def test_tool_list_and_annotations(mcp_client):
    tools = (await mcp_client.list_tools()).tools
    names = {t.name for t in tools}
    assert names == READ_TOOLS
    for t in tools:
        assert t.annotations and t.annotations.read_only_hint is True, t.name
        assert t.description and len(t.description.split(".")) >= 3, t.name


async def test_no_key_is_model_instruction(mcp_client, fake):
    result = await mcp_client.call_tool("account", {})
    assert result.is_error
    body = _payload(result)
    assert body["code"] == "no_api_key" and "console" in body["next_step"]
    assert "last" not in fake  # 클라이언트를 만들지도 않았다


async def test_account(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("account", {})
    assert not result.is_error, result.content
    body = _payload(result)
    assert body["user"]["email"] == "u@example.com" and body["credit"]["balance"] == 12.5
    assert "raw" not in body["user"]
    assert fake["last"].key.startswith("meshive_")
    assert fake["last"].calls[-1][0] == "close"


async def test_pods_pages_and_filters(mcp_client, fake, with_key):
    first = _payload(await mcp_client.call_tool("pods", {}))
    assert first["workspace"] == "ws-a" and len(first["items"]) == 20 and first["total"] == 45
    second = _payload(await mcp_client.call_tool("pods", {"cursor": first["next_cursor"], "limit": 100}))
    assert len(second["items"]) == 25 and second["next_cursor"] is None
    stopped = _payload(await mcp_client.call_tool("pods", {"status": "stopped", "limit": 100}))
    assert stopped["total"] == 23 and all(p["status"] == "stopped" for p in stopped["items"])


async def test_pods_single(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("pods", {"pod": "pod-7", "workspace": "ws-a"}))
    assert body["pod"]["pod_name"] == "pod-7"
    assert ("list_workspaces", (), {}) not in fake["last"].calls  # 명시된 워크스페이스는 조회 안 함


async def test_workspace_needs_input(mcp_client, fake, with_key):
    from conftest import _ws
    fake["setup"] = lambda inst: setattr(inst, "workspaces", [_ws("ws-a", "research"), _ws("ws-b", "prod")])
    body = _payload(await mcp_client.call_tool("pods", {}))
    assert body["needs_input"] == "workspace"
    assert [w["workspace"] for w in body["workspaces"]] == ["ws-a", "ws-b"]
    assert body["workspaces"][1]["label"] == "prod"


async def test_sdk_error_translated(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(list_pods=RateLimitError(429, "Too many", retry_after=7.0))
    result = await mcp_client.call_tool("pods", {"workspace": "ws-a"})
    assert result.is_error
    body = _payload(result)
    assert body["code"] == "rate_limited" and body["retry_after"] == 7


async def test_gpus_with_key_normalizes_fields(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("gpus", {"rental_type": "spot"}))
    assert body["authenticated"] is True
    row = body["items"][0]
    assert row["vram_gb"] == 32 and row["price_per_hour_usd"] == "0.6" and "vram" not in row
    assert row["max_gpus_per_pod"] == 2


async def test_gpus_rejects_bad_rental_type(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("gpus", {"rental_type": "hourly"})
    assert result.is_error and _payload(result)["code"] == "invalid_argument"


async def test_gpus_without_key_uses_public_catalog(mcp_client, fake, monkeypatch):
    import httpx
    from meshive_mcp.tools import gpus as gpus_module

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/landing/pricing-catalog"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"gpu_pod_prices": [
            {"model": "RTX 5090", "vram": 32, "demand_price": "0.6", "spot_price": "0.3"},
            {"model": "RTX 3060", "vram": 12, "demand_price": "0.1", "spot_price": "0.05"}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(gpus_module.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    body = _payload(await mcp_client.call_tool("gpus", {"rental_type": "spot", "min_vram_gb": 16}))
    assert body["authenticated"] is False
    assert body["items"] == [{"gpu_model": "RTX 5090", "vram_gb": 32, "rental_type": "spot",
                              "price_per_hour_usd": "0.3", "availability": "unknown"}]
