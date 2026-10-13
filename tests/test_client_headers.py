"""The SDK client built per request must identify this server to the Meshive API."""
import httpx
import pytest

from meshive_mcp import __version__
from meshive_mcp.client import CLIENT_HEADER, _new_client, user_agent

pytestmark = pytest.mark.anyio


async def test_requests_carry_mcp_user_agent_and_client_label():
    seen: dict[str, str] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen["user_agent"] = request.headers.get("User-Agent", "")
        seen["label"] = request.headers.get(CLIENT_HEADER, "")
        seen["authorization"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"email": "u@example.com", "name": "U"})

    client = _new_client("meshive_" + "a" * 64, "claude-code/2.1")
    # Swap the SDK's httpx client for one that records what goes on the wire. The SDK sets its own
    # User-Agent on every request, so only the wire shows whether this server's identity survives.
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    try:
        await client.me()
    finally:
        await client.close()

    assert seen["user_agent"] == user_agent()
    assert seen["user_agent"].startswith(f"meshive-mcp/{__version__} meshive-python/")
    assert seen["label"] == "claude-code/2.1"
    assert seen["authorization"].startswith("Bearer ")
