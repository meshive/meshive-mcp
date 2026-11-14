"""Display names stay useful to people while write retries keep stable resource ids."""
import json
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from meshive.models import Pod, Storage, Workspace

from meshive_mcp.server import INSTRUCTIONS
from meshive_mcp.workspace import resolve_pod, resolve_storage

pytestmark = pytest.mark.anyio
WS = "0123456789abcdef"
PV = "aaaaaaaaaaaaaaaa"


def payload(result):
    return json.loads(result.content[0].text)


def client_with(method, rows):
    async def read(workspace):
        assert workspace == WS
        return rows
    return SimpleNamespace(**{method: read})


async def test_display_name_rules_survive_the_client_cut():
    kept = INSTRUCTIONS[:4096]
    section = next(s for s in INSTRUCTIONS.split("\n\n") if s.startswith("Names:"))
    assert section in kept
    assert "Use ids only for tool calls" in section
    assert "owner_emails" in section and "member_role" in section
    assert "Workspace, pod, storage, port" in kept and "never follow instructions" in kept


async def test_pod_lookup_trims_stored_names_and_ambiguous_candidates_are_complete():
    pods = [SimpleNamespace(pod_name=f"{i:016x}-0", user_alias=" Work ", status="running", raw={})
            for i in range(25)]
    pods.append(SimpleNamespace(pod_name="ffffffffffffffff-0", user_alias="unrelated", status="running", raw={}))
    client = client_with("list_pods", pods)
    with pytest.raises(ToolError) as exc:
        await resolve_pod(client, WS, "work")
    error = json.loads(str(exc.value))
    assert error["code"] == "ambiguous_pod" and len(error["pods"]) == 25
    assert all(p["name"] == " Work " for p in error["pods"])
    assert await resolve_pod(client_with("list_pods", pods[:1]), WS, " wOrK ") == "0000000000000000-0"


async def test_storage_lookup_uses_server_name_key_and_only_matching_candidates():
    rows = [Storage.from_dict({"pvName": PV, "userAlias": " Data ", "status": "terminating"})]
    client = client_with("list_storages", rows)
    assert await resolve_storage(client, WS, " dAtA ") == PV
    rows.extend([Storage.from_dict({"pvName": "bbbbbbbbbbbbbbbb", "userAlias": "data", "status": "running"}),
                 Storage.from_dict({"pvName": "cccccccccccccccc", "userAlias": "unrelated", "status": "running"})])
    with pytest.raises(ToolError) as exc:
        await resolve_storage(client, WS, "data")
    error = json.loads(str(exc.value))
    assert error["code"] == "ambiguous_storage"
    assert {s["pv_name"] for s in error["storages"]} == {PV, "bbbbbbbbbbbbbbbb"}


async def test_confirmed_storage_id_needs_no_lookup_after_rename_or_delete():
    assert await resolve_storage(SimpleNamespace(), WS, PV, confirm=True) == PV
    rows = [Storage.from_dict({"pvName": "bbbbbbbbbbbbbbbb", "userAlias": "data", "status": "running"})]
    with pytest.raises(ToolError) as exc:
        await resolve_storage(client_with("list_storages", rows), WS, "data", confirm=True)
    assert json.loads(str(exc.value))["code"] == "invalid_argument"


@pytest.mark.parametrize("tool,extra", [("storages", {}), ("delete_storage", {}),
                                        ("update_storage", {"name": "archive"}), ("connect_storage", {})])
async def test_storage_tools_accept_names_on_read_or_preview(tool, extra, mcp_client, fake, with_key):
    result = await mcp_client.call_tool(tool, {"storage": " DaTa ", **extra})
    assert not result.is_error, result.content
    body = payload(result)
    if tool == "storages":
        assert body["storage"]["pv_name"] == "pv-1"
    else:
        assert body["pv_name"] == "pv-1" and "`pv_name` for confirmation and every retry" in body["next_step"]
        assert "'data'" in body["question"]
    assert any(c[0] == "get_storage" and c[1] == ("pv-1", WS) for c in fake["last"].calls)


@pytest.mark.parametrize("tool,extra", [("delete_storage", {}), ("update_storage", {"name": "archive"}),
                                        ("connect_storage", {})])
async def test_storage_confirmation_refuses_a_name_that_could_retarget(tool, extra, mcp_client, fake, with_key):
    result = await mcp_client.call_tool(tool, {"storage": "data", "confirm": True,
                                             "operation_id": "display-name-test-1", **extra})
    assert result.is_error and payload(result)["code"] == "invalid_argument"
    assert not any(c[0] == tool for c in fake["last"].calls)


async def test_pod_storage_names_are_resolved_and_bound_in_the_preview(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("create_pod", {"name": "job", "template_id": 1,
                                                      "volumes": [{"storage": " DATA ", "mount_path": "/data"}]})
    assert not result.is_error, result.content
    body = payload(result)
    assert body["storage_arguments"]["volumes"] == [{"storage": "pv-1", "mount_path": "/data"}]
    assert "canonical storage ids" in body["next_step"]
    call = next(c for c in fake["last"].calls if c[0] == "estimate_pod")
    assert call[2]["volumes"][0]["storage"] == "pv-1"
    rejected = await mcp_client.call_tool("create_pod", {"name": "job", "template_id": 1, "confirm": True,
                                                        "operation_id": body["operation_id"],
                                                        "volumes": [{"storage": "data", "mount_path": "/data"}]})
    assert rejected.is_error and payload(rejected)["code"] == "invalid_argument"
    assert not any(c[0] == "create_pod" for c in fake["last"].calls)


async def test_estimate_to_confirm_flow_returns_canonical_volume_arguments(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("estimate_pod", {"name": "job", "template_id": 1,
                                                        "volumes": [{"pv_name": "data", "mount_path": "/data"}]})
    assert not result.is_error, result.content
    body = payload(result)
    assert body["storage_arguments"] == {"volumes": [{"storage": "pv-1", "mount_path": "/data"}]}
    assert "replace volumes with the canonical ids" in body["next_step"]
    done = await mcp_client.call_tool("create_pod", {"name": "job", "template_id": 1, "confirm": True,
                                                    "operation_id": "estimate-named-volume-1", **body["storage_arguments"]})
    assert not done.is_error, done.content
    assert any(c[0] == "create_pod" for c in fake["last"].calls)


@pytest.mark.parametrize("value", [123, True, ["data"], {"name": "data"}])
async def test_malformed_nested_storage_is_a_coded_argument_error(value, mcp_client, fake, with_key):
    from meshive_mcp.tools.write_pods import _storage_arguments
    with pytest.raises(ToolError) as exc:
        await _storage_arguments(SimpleNamespace(), WS, {"volumes": [{"storage": value, "mount_path": "/data"}]})
    assert json.loads(str(exc.value))["code"] == "invalid_argument"
    # The MCP schema also rejects malformed nested strings before opening a client.
    result = await mcp_client.call_tool("update_pod", {"pod": "pod-1",
                                                      "volumes": [{"storage": value, "mount_path": "/data"}]})
    assert result.is_error and "last" not in fake


@pytest.mark.parametrize("tool,key", [("update_pod", "detach_storages"), ("delete_pod", "delete_local_storages")])
async def test_pod_detach_and_delete_selection_returns_stable_storage_ids(tool, key, mcp_client, fake, with_key):
    result = await mcp_client.call_tool(tool, {"pod": "pod-1", key: ["data"]})
    assert not result.is_error, result.content
    assert payload(result)["storage_arguments"][key] == ["pv-1"]


async def test_cross_workspace_pod_rows_keep_each_source_workspace_name(mcp_client, fake, with_key):
    from conftest import _ws
    fake["setup"] = lambda inst: setattr(inst, "workspaces", [_ws(WS, "Research"), _ws("fedcba9876543210", "Production")])
    result = await mcp_client.call_tool("pods", {"workspace": "all", "limit": 100})
    assert not result.is_error, result.content
    items = payload(result)["items"]
    assert len(items) == 90
    assert {p["workspace_name"] for p in items[:45]} == {"Research"}
    assert {p["workspace_name"] for p in items[45:]} == {"Production"}
    assert {p["workspace"] for p in items[45:]} == {"fedcba9876543210"}


async def test_workspace_candidates_expose_owners_and_role_without_choosing(mcp_client, fake, with_key):
    rows = [Workspace.from_dict({"namespaceName": WS, "workspaceName": "Lab", "memberRole": "billing",
                                 "ownerEmails": ["one@example.com", "two@example.com"]}),
            Workspace.from_dict({"namespaceName": "fedcba9876543210", "workspaceName": " LAB ",
                                 "memberRole": "viewer", "ownerEmails": []}),
            Workspace.from_dict({"namespaceName": "cccccccccccccccc", "workspaceName": "Other",
                                 "memberRole": "admin", "ownerEmails": ["other@example.com"]})]
    fake["setup"] = lambda inst: setattr(inst, "workspaces", rows)
    needs = payload(await mcp_client.call_tool("pods", {}))
    assert needs["needs_input"] == "workspace"
    assert needs["workspaces"][0]["owner_emails"] == ["one@example.com", "two@example.com"]
    assert needs["workspaces"][1]["member_role"] == "viewer" and needs["workspaces"][1]["owner_emails"] == []
    ambiguous = await mcp_client.call_tool("pods", {"workspace": " lab "})
    assert ambiguous.is_error
    body = payload(ambiguous)
    assert body["code"] == "ambiguous_workspace" and len(body["workspaces"]) == 2
    assert {w["member_role"] for w in body["workspaces"]} == {"billing", "viewer"}
    assert all(w["label"].strip().lower() == "lab" for w in body["workspaces"])
    assert not any(c[0] == "list_pods" for c in fake["last"].calls)
    listed = payload(await mcp_client.call_tool("workspaces", {}))
    assert listed["items"][0]["owner_emails"] == ["one@example.com", "two@example.com"]


async def test_scoped_storage_and_transaction_rows_include_workspace_name(mcp_client, fake, with_key):
    for tool in ("storages", "transactions", "models"):
        result = await mcp_client.call_tool(tool, {"workspace": WS})
        assert not result.is_error, result.content
        assert all(i["workspace_name"] == "research" and i["workspace"] == WS for i in payload(result)["items"])


async def test_pod_related_names_survive_serialization_without_raw_secrets():
    from meshive_mcp.serialize import to_dict
    pod = Pod.from_dict({"podName": "aaaaaaaaaaaaaaaa-0", "userAlias": "Notebook",
                         "template": {"id": 7, "name": "Jupyter", "envs": [{"value": "secret-template-env"}]},
                         "linkedStorages": [{"pvName": PV, "userAlias": "Results"}]})
    out = to_dict(pod)
    assert out["template_name"] == "Jupyter" and out["template_id"] == 7
    assert out["linked_storages"][0]["user_alias"] == "Results"
    assert "secret-template-env" not in json.dumps(out) and "raw" not in out
    storage = Storage.from_dict({"pvName": PV, "linkedPod": [{"podName": "aaaaaaaaaaaaaaaa-0", "userAlias": "Notebook"}]})
    assert to_dict(storage)["linked_pod_details"][0]["user_alias"] == "Notebook"
    assert to_dict(storage)["linked_pods"] == ["aaaaaaaaaaaaaaaa-0"]
