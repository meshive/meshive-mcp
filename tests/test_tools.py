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
    assert first["workspace"] == "0123456789abcdef" and len(first["items"]) == 20 and first["total"] == 45
    second = _payload(await mcp_client.call_tool("pods", {"cursor": first["next_cursor"], "limit": 100}))
    assert len(second["items"]) == 25 and second["next_cursor"] is None
    stopped = _payload(await mcp_client.call_tool("pods", {"status": "stopped", "limit": 100}))
    assert stopped["total"] == 23 and all(p["status"] == "stopped" for p in stopped["items"])


async def test_pods_single(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("pods", {"pod": "pod-7", "workspace": "0123456789abcdef"}))
    assert body["pod"]["pod_name"] == "pod-7"
    assert ("list_workspaces", (), {}) not in fake["last"].calls  # 명시된 워크스페이스는 조회 안 함


async def test_workspace_needs_input(mcp_client, fake, with_key):
    from conftest import _ws
    fake["setup"] = lambda inst: setattr(inst, "workspaces", [_ws("0123456789abcdef", "research"), _ws("fedcba9876543210", "prod")])
    body = _payload(await mcp_client.call_tool("pods", {}))
    assert body["needs_input"] == "workspace"
    assert [w["workspace"] for w in body["workspaces"]] == ["0123456789abcdef", "fedcba9876543210"]
    assert body["workspaces"][1]["label"] == "prod"


async def test_sdk_error_translated(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(list_pods=RateLimitError(429, "Too many", retry_after=7.0))
    result = await mcp_client.call_tool("pods", {"workspace": "0123456789abcdef"})
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


def _two_ws(inst):
    from conftest import _ws
    inst.workspaces = [_ws("aaaaaaaaaaaaaaaa", "research"), _ws("bbbbbbbbbbbbbbbb", "prod")]


async def test_workspace_label_is_resolved_to_id(mcp_client, fake, with_key):
    fake["setup"] = _two_ws
    body = _payload(await mcp_client.call_tool("pods", {"workspace": "Prod", "limit": 5}))
    assert body["workspace"] == "bbbbbbbbbbbbbbbb"
    assert ("list_pods", ("bbbbbbbbbbbbbbbb",), {}) in fake["last"].calls


async def test_unknown_label_lists_candidates(mcp_client, fake, with_key):
    fake["setup"] = _two_ws
    result = await mcp_client.call_tool("pods", {"workspace": "nope"})
    assert result.is_error
    body = _payload(result)
    assert body["code"] == "unknown_workspace"
    assert [w["label"] for w in body["workspaces"]] == ["research", "prod"]


async def test_workspace_all_merges_every_workspace(mcp_client, fake, with_key):
    fake["setup"] = _two_ws
    body = _payload(await mcp_client.call_tool("pods", {"workspace": "all", "limit": 100}))
    assert body["workspace"] == "all" and body["total"] == 90  # 45 pods × 2 workspaces
    assert [c[0] for c in fake["last"].calls].count("list_pods") == 2


async def test_workspace_all_rejected_where_unsupported(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("tasks", {"workspace": "all"})
    assert result.is_error and _payload(result)["code"] == "invalid_argument"


async def test_not_a_member_403_becomes_unknown_workspace(mcp_client, fake, with_key):
    from meshive.exceptions import PermissionDeniedError
    fake["setup"] = lambda inst: inst.raise_on.update(
        list_pods=PermissionDeniedError(403, "You are not a member of workspace"))
    result = await mcp_client.call_tool("pods", {"workspace": "cccccccccccccccc"})
    assert result.is_error
    body = _payload(result)
    assert body["code"] == "unknown_workspace" and "workspaces tool" in body["next_step"]


async def test_output_is_compact_json_with_structured_content(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("account", {})
    text = result.content[0].text
    assert "\n" not in text and ": " not in text
    assert result.structured_content["user"]["email"] == "u@example.com"


def test_number_normalization():
    from meshive_mcp.serialize import normalize_number, to_dict
    assert normalize_number("0E-8") == "0"
    assert normalize_number("0.87741500") == "0.877415"
    assert normalize_number("1.2E+2") == "120"
    assert normalize_number("457") == "457"          # 정수 문자열(id 일 수 있음)은 그대로
    assert normalize_number("task_1e5") == "task_1e5"
    assert to_dict({"price_per_hour": "0E-8", "name": "x"}) == {"price_per_hour": "0", "name": "x"}
