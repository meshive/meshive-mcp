"""A confirmed pod write acts on the pod the user saw in the preview, never on whichever pod has that name now.

A display name can move between preview and confirmation: another member renames the pod, or deletes it and a new
pod takes the name. The preview returns the pod's `pod_name`; confirm=true and every retry must send that id.
"""
import json

import pytest
from meshive.models import Pod

from conftest import LOCK_ENDPOINTS

pytestmark = pytest.mark.anyio

A = "aaaaaaaaaaaaaaaa-0"
B = "bbbbbbbbbbbbbbbb-0"


def payload(result):
    return json.loads(result.content[0].text)


def pod(pod_name: str, name: str, status: str = "running") -> Pod:
    return Pod.from_dict({"podName": pod_name, "namespaceName": "0123456789abcdef", "userAlias": name,
                          "status": status, "rentalType": "demand", "pricePerHour": "0.5",
                          "isMaintenance": False, "isDownloader": False})


def shared(fake, pods):
    """Every tool call builds its own client; give them one pod list and one call log."""
    state = {"pods": pods, "calls": []}

    def setup(inst):
        inst.pods = state["pods"]
        inst.calls = state["calls"]
    fake["setup"] = setup
    return state


def sent(state, method):
    return [call for call in state["calls"] if call[0] == method]


CASES = [
    ("delete_pod", {}),
    ("start_pod", {}),
    ("update_pod", {"vcpu": 8}),
    ("set_watched_folders", {"expected_version": 1, "user": ["/workspace/new-results"]}),
]


@pytest.mark.parametrize("tool,extra", CASES)
async def test_confirm_by_name_after_the_name_moved_reaches_no_other_pod(tool, extra, mcp_client, fake, with_key):
    state = shared(fake, [pod(A, "trainer"), pod(B, "scratch", "stopped")])
    preview = await mcp_client.call_tool(tool, {"pod": "trainer", **extra})
    assert not preview.is_error, preview.content
    op = payload(preview)["operation_id"]

    # Between preview and confirmation another member renames A and gives its name to B.
    state["pods"][:] = [pod(A, "trainer-old"), pod(B, "trainer", "stopped")]
    confirmed = await mcp_client.call_tool(tool, {"pod": "trainer", "confirm": True, "operation_id": op, **extra})

    hit_b = [call for call in sent(state, tool) if B in call[1]]
    assert not hit_b, f"confirm=true by name acted on pod B: {hit_b}"
    assert confirmed.is_error and payload(confirmed)["code"] == "invalid_argument"


@pytest.mark.parametrize("tool,extra", CASES)
async def test_preview_returns_the_pod_id_to_confirm_with(tool, extra, mcp_client, fake, with_key):
    shared(fake, [pod(A, "trainer"), pod(B, "scratch", "stopped")])
    body = payload(await mcp_client.call_tool(tool, {"pod": "trainer", **extra}))
    assert body.get("pod_name") == A
    assert "`pod_name`" in body["next_step"]


@pytest.mark.parametrize("tool,extra", CASES)
async def test_confirm_with_the_previewed_id_still_works(tool, extra, mcp_client, fake, with_key):
    state = shared(fake, [pod(A, "trainer-old"), pod(B, "trainer", "stopped")])
    result = await mcp_client.call_tool(tool, {"pod": A, "confirm": True, "operation_id": "op-confirm-0001", **extra})
    assert not result.is_error, result.content
    assert any(A in call[1] for call in sent(state, tool))


def pod_with_storage(local: list[tuple[str, str, int]], network: list[tuple[str, str]] = ()) -> Pod:
    storages = [{"pvName": pv, "userAlias": name, "mountPath": f"/mnt/{name}", "capacity": mib,
                 "storageType": "hostPath"} for pv, name, mib in local]
    storages += [{"pvName": pv, "userAlias": name, "mountPath": f"/mnt/{name}", "capacity": 20480,
                  "storageType": "nfs"} for pv, name in network]
    return Pod.from_dict({"podName": A, "namespaceName": "0123456789abcdef", "userAlias": "trainer",
                          "status": "running", "rentalType": "demand", "pricePerHour": "0.5",
                          "isMaintenance": False, "isDownloader": False, "linkedStorages": storages})


async def test_delete_preview_names_the_local_storage_it_deletes_and_the_one_it_keeps(mcp_client, fake, with_key):
    from meshive.models import Storage
    current = pod_with_storage([("1111111111111111", "scratch", 10240), ("2222222222222222", "cache", 51200)],
                               network=[("3333333333333333", "datasets")])

    def setup(inst):
        inst.pods = [current]

        async def get_pod(pod, ws):
            inst._rec("get_pod", pod, ws)
            return current

        async def list_storages(ws):
            return [Storage.from_dict({"pvName": "1111111111111111", "userAlias": "scratch", "status": "running"}),
                    Storage.from_dict({"pvName": "2222222222222222", "userAlias": "cache", "status": "running"})]
        inst.get_pod = get_pod
        inst.list_storages = list_storages
    fake["setup"] = setup

    body = payload(await mcp_client.call_tool("delete_pod", {"pod": "trainer", "delete_local_storages": ["scratch"]}))
    question = body["question"]
    assert question == ("Delete pod 'trainer' (running) and its local volume 'scratch' (10 GiB) with all data on it? "
                        "This cannot be undone. Its local volume 'cache' (50 GiB) is kept and stays billed until it "
                        "is deleted."), question
    assert "delete_local_storages" not in question, "agent instructions belong in next_step, not in the user's question"
    assert "add it to `delete_local_storages`" in body["next_step"]
    assert "datasets" not in question, "network storage survives the pod and isn't a local volume"
    assert body["storage_arguments"] == {"delete_local_storages": ["1111111111111111"]}
    assert body["pod_name"] == A

    plain = payload(await mcp_client.call_tool("delete_pod", {"pod": A}))
    assert plain["question"].startswith("Delete pod 'trainer' (running)? This cannot be undone.")
    assert "Its local volumes 'scratch' (10 GiB) and 'cache' (50 GiB) are kept and stay billed" in plain["question"]

    both = payload(await mcp_client.call_tool(
        "delete_pod", {"pod": A, "delete_local_storages": ["scratch", "cache"]}))
    assert both["question"] == ("Delete pod 'trainer' (running) and its local volumes 'scratch' (10 GiB) and "
                                "'cache' (50 GiB) with all data on them? This cannot be undone.")
    assert "add it to" not in both["next_step"]


# Port locks and access keys confirm the same way. Unlocking another pod's port opens it to anyone who knows its URL,
# and a key created on another pod lets programs into that pod.
ENDPOINT_CASES = [
    ("set_port_lock", {"port": 8188, "locked": False}),
    ("create_access_key", {"port": 8188, "name": "batch-script"}),
    ("roll_access_key", {"port": 8188, "key_id": 12}),
    ("revoke_access_key", {"port": 8188, "key_id": 12}),
]


def shared_ports(fake, pods):
    state = shared(fake, pods)
    plain = fake["setup"]

    def setup(inst):
        plain(inst)
        inst.endpoints = LOCK_ENDPOINTS
    fake["setup"] = setup
    return state


@pytest.mark.parametrize("tool,extra", ENDPOINT_CASES)
async def test_port_confirm_by_name_after_the_name_moved_reaches_no_other_pod(tool, extra, mcp_client, fake,
                                                                             with_key):
    state = shared_ports(fake, [pod(A, "web"), pod(B, "scratch")])
    preview = await mcp_client.call_tool(tool, {"pod": "web", **extra})
    assert not preview.is_error, preview.content
    op = payload(preview)["operation_id"]

    # Between preview and confirmation the pod is renamed and its name given to another pod.
    state["pods"][:] = [pod(A, "web-old"), pod(B, "web")]
    confirmed = await mcp_client.call_tool(tool, {"pod": "web", "confirm": True, "operation_id": op, **extra})

    assert not sent(state, tool), f"confirm=true by name sent {tool}: {sent(state, tool)}"
    refused = payload(confirmed)
    assert confirmed.is_error and refused["code"] == "invalid_argument"
    # The way back is the id the user agreed to; a new preview by name may reach the other pod, so it needs a new yes.
    assert "`pod_name` returned by the preview the user agreed to" in refused["next_step"]
    assert "preview again and ask the user again" in refused["next_step"]


@pytest.mark.parametrize("tool,extra", ENDPOINT_CASES)
async def test_port_preview_returns_the_pod_id_to_confirm_with(tool, extra, mcp_client, fake, with_key):
    shared_ports(fake, [pod(A, "web"), pod(B, "scratch")])
    body = payload(await mcp_client.call_tool(tool, {"pod": "web", **extra}))
    assert body.get("pod_name") == A
    assert f"`pod_name` ({A})" in body["next_step"]


async def test_expired_key_roll_preview_returns_the_pod_id_too(mcp_client, fake, with_key):
    from conftest import access_key_item
    shared_ports(fake, [pod(A, "web")])
    base = fake["setup"]

    def setup(inst):
        base(inst)
        inst.access_keys = {8188: [{**access_key_item(14, "old-ci"), "expiresAt": "2026-11-10T09:00:00Z",
                                    "expired": True}]}
    fake["setup"] = setup
    body = payload(await mcp_client.call_tool("roll_access_key", {"pod": "web", "port": 8188, "key_id": 14}))
    assert body["question"].startswith("Roll expired access key 'old-ci'"), body["question"]
    assert body["pod_name"] == A and f"`pod_name` ({A}) for the confirmation;" in body["next_step"]


@pytest.mark.parametrize("tool,extra", ENDPOINT_CASES)
async def test_port_confirm_with_the_previewed_id_still_works(tool, extra, mcp_client, fake, with_key):
    state = shared_ports(fake, [pod(A, "web-old"), pod(B, "web")])
    result = await mcp_client.call_tool(tool, {"pod": A, "confirm": True, "operation_id": "op-confirm-0001", **extra})
    assert not result.is_error, result.content
    assert [call[1][0] for call in sent(state, tool)] == [A]


async def test_key_previews_bind_the_confirmation_without_inviting_a_retry(mcp_client, fake, with_key):
    # A second create or roll call issues a second value, so these previews never mention retrying.
    shared_ports(fake, [pod(A, "web")])
    for tool, extra in ENDPOINT_CASES:
        if tool not in ("create_access_key", "roll_access_key"):
            continue
        step = payload(await mcp_client.call_tool(tool, {"pod": "web", **extra}))["next_step"]
        assert f"Set `pod` to the returned `pod_name` ({A}) for the confirmation;" in step
        assert "retry" not in step and step.index("`pod_name`") < step.index("don't call it again"), step
        assert step.endswith(" Reuse this operation_id for the confirmation."), step


@pytest.mark.parametrize("confirm", [False, True])
async def test_locking_by_display_name_still_works(confirm, mcp_client, fake, with_key):
    # A lock has no preview, so there is nothing to bind; an unneeded confirm=true doesn't refuse the name.
    state = shared_ports(fake, [pod(A, "web"), pod(B, "scratch")])
    args = {"pod": "web", "port": 8188, "locked": True, "operation_id": "op-lock-0001"}
    result = await mcp_client.call_tool("set_port_lock", {**args, "confirm": confirm})
    assert not result.is_error, result.content
    assert [call[1][:4] for call in sent(state, "set_port_lock")] == [(A, "0123456789abcdef", 8188, True)]
