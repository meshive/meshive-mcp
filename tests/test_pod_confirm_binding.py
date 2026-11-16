"""A confirmed pod write acts on the pod the user saw in the preview, never on whichever pod has that name now.

A display name can move between preview and confirmation: another member renames the pod, or deletes it and a new
pod takes the name. The preview returns the pod's `pod_name`; confirm=true and every retry must send that id.
"""
import json

import pytest
from meshive.models import Pod

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
