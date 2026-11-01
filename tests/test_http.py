"""HTTP transport: /healthz, Bearer header passthrough, GET /mcp rejection."""
import json

import anyio
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
        # Run the lifespan directly — ASGITransport doesn't send the startup event.
        async with app.router.lifespan_context(app):
            yield c


async def test_healthz(http, monkeypatch):
    """Deployment-verification contract: the MCP/SDK revisions baked into the image and the installed SDK version show as-is. null in local builds."""
    import meshive
    monkeypatch.setenv("MESHIVE_MCP_REVISION", "abc123")
    monkeypatch.setenv("MESHIVE_SDK_REVISION", "def456")
    body = (await http.get("/healthz")).json()
    assert body["status"] == "ok" and body["revision"] == "abc123" and body["sdk_revision"] == "def456"
    assert body["sdk_version"] == meshive.__version__
    monkeypatch.delenv("MESHIVE_MCP_REVISION")
    assert (await http.get("/healthz")).json()["revision"] is None


async def test_get_mcp_is_not_a_stream(http):
    r = await http.get("/mcp", headers={"Accept": "text/event-stream"})
    assert r.status_code == 405


@pytest.mark.parametrize("method, path, headers, body", [
    ("GET", "/healthz", {}, None),
    ("GET", "/healthz", {"MCP-Protocol-Version": "2025-06-18"}, None),
    ("GET", "/mcp", {}, None),
    ("GET", "/.well-known/oauth-protected-resource/mcp", {}, None),
    ("POST", "/mcp", HEADERS, _rpc("ping")),
    ("POST", "/mcp", HEADERS, _rpc("notifications/initialized", id=None)),
])
async def test_responses_carry_no_content_length(http, method, path, headers, body):
    """Avast and Norton break MCP responses that have a Content-Length, even short and empty ones."""
    r = await http.request(method, path, headers=headers, json=body)
    assert "content-length" not in r.headers


async def test_body_over_the_limit_is_refused(http):
    """Deployments don't buffer requests at the proxy, so this limit is what keeps a body out of memory."""
    limit = 4 * 1024 * 1024

    async def in_pieces():
        for _ in range(5):
            yield b"x" * (1024 * 1024)

    assert (await http.post("/mcp", headers=HEADERS, content=b"x" * (limit + 1))).status_code == 413
    assert (await http.post("/mcp", headers=HEADERS, content=in_pieces())).status_code == 413


async def test_body_within_the_limit_reaches_the_server(http):
    async def in_pieces():
        raw = json.dumps(_rpc("ping")).encode()
        yield raw[:10]
        yield raw[10:]

    r = await http.post("/mcp", headers=HEADERS, content=in_pieces())
    assert r.status_code == 200 and r.json()["id"] == 1


MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test-agent", "version": "9.9"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


async def _modern_post(http, method, **params):
    headers = {**HEADERS, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    with anyio.fail_after(5):
        return await http.post("/mcp", headers=headers, json=_rpc(method, {"_meta": MODERN_META, **params}))


async def test_discover_offers_no_change_notifications(http):
    """A client that sees listChanged opens subscriptions/listen, and waits on it if a proxy buffers the stream."""
    r = await _modern_post(http, "server/discover")
    assert r.status_code == 200, r.text
    capabilities = r.json()["result"]["capabilities"]
    assert capabilities["tools"]["listChanged"] is False
    assert capabilities["prompts"]["listChanged"] is False
    assert capabilities["resources"]["listChanged"] is False


async def test_listen_is_refused_instead_of_held_open(http):
    r = await _modern_post(http, "subscriptions/listen", notifications={"toolsListChanged": True})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == -32601


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
    # Stateless, so this request has no clientInfo → User-Agent fallback
    assert fake["last"].label in ("test-agent/9.9", "codex/1.0")


async def test_missing_bearer_is_tool_error_not_http_401(http, fake):
    r = await http.post("/mcp", headers=HEADERS, json=_rpc("tools/call", {"name": "account", "arguments": {}}))
    assert r.status_code == 200
    payload = r.json()["result"]
    assert payload["isError"] and json.loads(payload["content"][0]["text"])["code"] == "no_api_key"
