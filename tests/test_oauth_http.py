import hashlib

import httpx
import pytest

from test_http import HEADERS, _rpc, http
from oauth_tool_cases import CASES
from meshive_mcp import oauth
from meshive_mcp.settings import settings

pytestmark = pytest.mark.anyio
RESOURCE = "https://mcp.example.test/mcp"
ISSUER = "https://auth.example.test"


@pytest.fixture
def private(monkeypatch):
    monkeypatch.setattr(settings, "oauth_resource", RESOURCE)
    monkeypatch.setattr(settings, "oauth_issuer", ISSUER)
    monkeypatch.setattr(settings, "oauth_internal_url", "https://private.test")
    calls = []

    async def handle(request):
        calls.append(request)
        token = request.headers["authorization"].split()[-1]
        if request.url.path.endswith("/inspect"):
            if token == "mshoa_expired":
                return httpx.Response(401)
            if token == "mshoa_unavailable":
                return httpx.Response(503)
            return httpx.Response(200, json={"scope": "read" if token.endswith("reader") else "read write"})
        if "/previews/" in request.url.path:
            return httpx.Response(200, json={"bindings": {}})
        if request.url.path.endswith("/exchange"):
            return httpx.Response(200, json={"credential": "mshod_bound"})
        if request.url.path.endswith(("/api-keys", "/platform-notices")):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"email": "oauth@example.test", "username": "owner", "namespaces": []})
    monkeypatch.setattr(oauth, "private_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                                                         base_url=settings.oauth_internal_url))
    return calls


async def test_discovery_and_tool_schemes(private, http):
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        metadata = (await http.get(path)).json()
        assert metadata["resource"] == RESOURCE and metadata["authorization_servers"] == [ISSUER]
    tools = (await http.post("/mcp", headers=HEADERS, json=_rpc("tools/list"))).json()["result"]["tools"]
    assert len(tools) == 98
    by_name = {t["name"]: t for t in tools}
    for case in CASES:
        t = by_name[case.tool]
        expected = ([{"type": "noauth"}, {"type": "oauth2", "scopes": ["read"]}] if case.scope == "public"
                    else [{"type": "oauth2", "scopes": case.scope.split()}])
        assert t["securitySchemes"] == t["_meta"]["securitySchemes"] == expected
    for name in oauth.ACTION_PATHS:
        assert "console_url" in by_name[name]["description"]
    assert by_name["gpus"]["securitySchemes"][0] == {"type": "noauth"}
    assert by_name["command_status"]["securitySchemes"][0]["scopes"] == ["read", "write"]


async def test_denial_precedes_execution(private, http):
    for key, tool, args, status in [
        (None, "gpus", {"rental_type": "TASK"}, 401), (None, "account", {}, 401),
        ("mshoa_expired", "gpus", {}, 401), ("bad", "gpus", {}, 401),
        ("mshoa_reader", "pods", {"show_secrets": "true"}, 403),
        ("mshoa_reader", "command_status", {}, 403), ("mshoa_unavailable", "account", {}, 503),
    ]:
        headers = {**HEADERS, **({"Authorization": "Bearer " + key} if key else {})}
        r = await http.post("/mcp", headers=headers, json=_rpc("tools/call", {"name": tool, "arguments": args}))
        assert r.status_code == status, r.text
        if status != 503:
            assert r.json()["result"]["_meta"]["mcp/www_authenticate"] == [r.headers["www-authenticate"]]
        else:
            assert "www-authenticate" not in r.headers
    assert all(req.url.path.endswith("/inspect") for req in private)


async def test_sdk_business_request_uses_only_an_exchanged_credential(private, http):
    r = await http.post("/mcp", headers={**HEADERS, "Authorization": "Bearer mshoa_reader"},
                        json=_rpc("tools/call", {"name": "account", "arguments": {}}))
    assert r.status_code == 200 and "oauth@example.test" in r.text, r.text
    exchanged = [req for req in private if req.url.path.endswith("/exchange")]
    import json
    assert any(json.loads(req.content) == {"tool": "account", "method": "GET", "target": "/v1/sdk/me",
        "operation_id": None, "body_hash": hashlib.sha256(b"").hexdigest()} for req in exchanged)
    assert all(req.headers["authorization"] == "Bearer mshod_bound" for req in private if req.url.path.startswith("/v1/"))


async def test_openai_tool_error_cannot_change_admission(private, http):
    r = await http.post("/mcp", headers=HEADERS, json=_rpc("tools/call", {
        "name": "pods", "arguments": {}, "_meta": {"openai/locale": "en-US"}}))
    assert r.status_code == 200 and r.json()["result"]["isError"]
    assert r.json()["result"]["_meta"]["mcp/www_authenticate"] == [r.headers["www-authenticate"]]
    assert private == []


async def test_enabled_public_catalog_and_manual_key_without_precheck(private, http, monkeypatch, fake):
    import meshive_mcp.tools.gpus as gpus
    from unittest.mock import AsyncMock
    monkeypatch.setattr(gpus, "_catalog_body", AsyncMock(return_value={"gpu_pod_prices": []}))
    public = await http.post("/mcp", headers=HEADERS, json=_rpc("tools/call", {"name": "gpus", "arguments": {"rental_type": "SPOT"}}))
    assert public.json()["result"]["structuredContent"]["authenticated"] is False
    assert private == []
    manual = "meshive_" + "c" * 64
    actual_client = httpx.AsyncClient
    def no_precheck(request):
        raise AssertionError("manual keys must reach the tool without an admission request")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: actual_client(transport=httpx.MockTransport(no_precheck), **kw))
    # The API checks manual keys per call, as before OAuth. Admission must not spend their rate limit.
    r = await http.post("/mcp", headers={**HEADERS, "Authorization": "Bearer " + manual},
                        json=_rpc("tools/call", {"name": "account", "arguments": {}}))
    assert r.status_code == 200
    assert fake["last"].key == manual and not private


async def test_disabled_oauth_keeps_public_and_manual_service_but_rejects_issued_tokens(http, monkeypatch, fake):
    from meshive_mcp.tools import gpus
    from unittest.mock import AsyncMock
    monkeypatch.setattr(settings, "oauth_resource", "")
    monkeypatch.setattr(gpus, "_catalog_body", AsyncMock(return_value={"gpu_pod_prices": []}))
    for token, expected in [(None, 200), ("mshoa_old", 401), ("mshod_old", 401)]:
        r = await http.post("/mcp", headers={**HEADERS, **({"Authorization": "Bearer " + token} if token else {})},
                            json=_rpc("tools/call", {"name": "gpus", "arguments": {}}))
        assert r.status_code == expected
    r = await http.post("/mcp", headers={**HEADERS, "Authorization": "Bearer meshive_manual"},
                        json=_rpc("tools/call", {"name": "account", "arguments": {}}))
    assert r.status_code == 200 and not r.json()["result"].get("isError")


@pytest.mark.parametrize("step", ["part-urls", "complete", "abort"])
async def test_collection_upload_capability_forwarded_only_to_bound_request(private, step):
    import json
    calls = []
    token = "local-upload-capability"
    target = "/v1/sdk/assets/asset-a/uploads/batch-b/" + step
    async def service(request):
        calls.append(request)
        if request.url.path.endswith("/exchange"):
            packet = json.loads(request.content)
            assert packet["upload_token_hash"] == hashlib.sha256(token.encode()).hexdigest()
            assert token.encode() not in request.content
            return httpx.Response(200, json={"credential": "mshod_bound"})
        assert request.url.path == target and request.headers["X-Asset-Upload-Token"] == token
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(base_url=settings.oauth_internal_url, transport=httpx.MockTransport(service)) as backend:
        transport = oauth.DelegatedTransport(backend, "mshoa_reader", "finish_upload")
        from meshive import AsyncMeshive
        sdk = AsyncMeshive("delegated", base_url=settings.oauth_internal_url, transport=transport, max_retries=0)
        try:
            if step == "part-urls":
                await sdk.asset_file_upload_part_urls("asset-a", "batch-b", token, "file-test", [1])
            elif step == "complete":
                await sdk.complete_asset_file_upload("asset-a", "batch-b", token)
            else:
                await sdk.abort_asset_file_upload("asset-a", "batch-b", token)
        finally:
            await sdk.close()
    assert len(calls) == 2
