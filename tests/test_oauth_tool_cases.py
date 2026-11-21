"""Check the OAuth acceptance cases against the tool list clients actually receive."""
from collections import Counter

import pytest
from jsonschema import validate
from meshive import AsyncMeshive

from test_templates_messages_tools import misc
from test_pod_assets_tools import pa_fake
from test_asset_files_tools import files_fake
from oauth_tool_cases import CASES, VARIANTS
from meshive_mcp.tools._common import _UNRECORDED, _WRITES


@pytest.mark.anyio
async def test_every_registered_tool_has_one_base_case(mcp_client):
    tools = (await mcp_client.list_tools()).tools
    assert len(tools) == 83
    assert Counter(t.name for t in tools) == Counter(case.tool for case in CASES)
    by_name = {tool.name: tool for tool in tools}
    for case in (*CASES, *VARIANTS):
        tool = by_name[case.tool]
        validate(case.arguments, tool.input_schema)
        assert set(case.arguments) <= tool.input_schema["properties"].keys(), case
        if case.scope == "public":
            assert case.tool == "gpus" and case.sdk is None
            continue
        assert callable(getattr(AsyncMeshive, case.sdk)), case
        if case.sdk in _WRITES:
            assert case.scope == "read write" and case.retry == "same operation ID", case
        elif case.sdk in _UNRECORDED:
            assert case.scope == "read write" and case.retry == "reconcile before retry", case
        else:
            assert case.effect == "none" and case.retry == "read again", case


def test_argument_sensitive_permissions_and_delivery():
    cases = (*CASES, *VARIANTS)
    assert {c.tool for c in cases if c.delivery == "console"} == {
        "pods", "ssh_access", "connect_storage", "machine_credentials", "host_install_command",
        "create_access_key", "roll_access_key",
    }
    assert {(c.arguments.get("rental_type", "demand"), c.scope) for c in cases if c.tool == "gpus"} == {
        ("demand", "public"), ("spot", "public"), ("demand", "read"), ("spot", "read"), ("task", "read"),
    }
    assert {(c.scope, c.delivery) for c in cases if c.tool == "pods"} == {
        ("read", "tool"), ("read write", "console"),
    }
    assert next(c.scope for c in CASES if c.tool == "command_status") == "read write"
    assert next(c.scope for c in CASES if c.tool == "estimate_pod") == "read"


@pytest.mark.anyio
@pytest.mark.parametrize("case", [c for c in (*CASES, *VARIANTS) if c.sdk], ids=lambda c: c.tool)
async def test_case_reaches_its_sdk_operation(case, mcp_client, fake, with_key, monkeypatch, request):
    """Stop at the expected SDK boundary, before it can produce a side effect."""
    from conftest import FakeMeshive
    from meshive.exceptions import AuthenticationError

    fixtures = {"save_template": "misc", "add_pod_asset": "pa_fake", "export_asset": "files_fake"}
    if case.tool in fixtures:
        request.getfixturevalue(fixtures[case.tool])
    calls = []

    async def stop_at_operation(self, *args, **kwargs):
        calls.append(kwargs)
        raise AuthenticationError(401, "Contract test stopped the SDK call")

    stop_at_operation.__name__ = case.sdk
    monkeypatch.setattr(FakeMeshive, case.sdk, stop_at_operation, raising=False)
    result = await mcp_client.call_tool(case.tool, case.arguments)
    assert len(calls) == 1, result.content
    if case.retry == "same operation ID":
        assert calls[0]["idempotency_key"] == case.arguments["operation_id"]
