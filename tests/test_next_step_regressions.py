"""Answers that told the agent the wrong next step: a reused operation_id, a timeout of a write the server keeps no
record of, exact-path copies out of large bucket folders, a registry login delete, the inference key lifetime and
role, and the note after an edit that removed input assets."""
import json

import httpx
import pytest
from meshive.exceptions import MeshiveAPIError
from meshive.models import Workspace

from test_assets_settings_tools import GIB, settings_fake  # noqa: F401  (fixture)
from test_inference_webhook_tools import state  # noqa: F401  (fixture)
from test_templates_messages_tools import misc  # noqa: F401  (fixture)

pytestmark = pytest.mark.anyio

WS = "0123456789abcdef"
OP = "op-second-review-0001"
TAIL = "MUST reuse this operation_id"


def _payload(result):
    return json.loads(result.content[0].text)


def _calls(fake, name):
    return [c for c in fake["last"].calls if c[0] == name] if "last" in fake else []


def _reused():
    title = "Idempotency-Key Reused"
    message = "This key is bound to different arguments. Check the original operation before submitting new work."
    return MeshiveAPIError(422, message, title=title, raw={"detail": {"title": title, "message": message}})


def _raise(fake, method, exc):
    fake["setup"] = lambda inst: inst.raise_on.update({method: exc})


# --- A reused operation_id ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool,method,args", [
    ("save_template", "update_template", {"template_id": 31, "workspace": WS, "name": "x"}),
    ("delete_template", "delete_template", {"template_id": 31, "workspace": WS, "confirm": True}),
    ("create_storage", "create_storage", {"name": "data", "size_gb": 10, "confirm": True}),
])
async def test_a_reused_operation_id_points_at_operation_status(mcp_client, fake, with_key, misc, tool, method, args):
    _raise(fake, method, _reused())
    body = _payload(await mcp_client.call_tool(tool, {**args, "operation_id": OP}))
    assert body["code"] == "operation_id_reused", body
    assert "operation_status" in body["next_step"] and "Fix the argument" not in body["next_step"]
    # "reuse the same arguments" contradicts a key that is bound to other arguments.
    assert TAIL not in body["next_step"] and body["operation_id"] == OP


# --- A timeout of a write the server keeps no record of -------------------------------------------------------------

async def test_an_unrecorded_write_that_timed_out_doesnt_point_at_operation_status(mcp_client, fake, with_key,
                                                                                   monkeypatch):
    async def run_command(self, pod, ws, command, *, timeout=20, background=False):
        raise httpx.ReadTimeout("timed out")

    fake["setup"] = lambda inst: monkeypatch.setattr(type(inst), "run_command", run_command)
    body = _payload(await mcp_client.call_tool("run_command", {"pod": "pod-1", "command": "ls", "operation_id": OP}))
    assert body["code"] == "temporarily_unavailable"
    assert "operation_status" not in body["next_step"] and "runs it again" in body["next_step"]


async def test_a_recorded_write_that_timed_out_still_points_at_operation_status(mcp_client, fake, with_key):
    _raise(fake, "create_storage", httpx.ReadTimeout("timed out"))
    body = _payload(await mcp_client.call_tool("create_storage", {"name": "data", "size_gb": 10, "confirm": True,
                                                                  "operation_id": OP}))
    assert "operation_status" in body["next_step"] and TAIL in body["next_step"]


# --- Exact paths out of a large bucket folder -----------------------------------------------------------------------

async def test_a_truncated_server_preview_of_exact_paths_reads_the_folder(mcp_client, fake, with_key, settings_fake):
    # The server's preview scans the whole linked folder and stops early; the exact-path copy (object keys) has no
    # such limit, so a truncated preview says nothing about whether the paths exist.
    settings_fake["subset"].update(matchedCount=0, totalSizeBytes=0, fetchable=False, truncated=True,
                                   sampleRelativePaths=[], limitNotice="The selection could not be listed completely")
    settings_fake["reference_listing"].update(nextContinuationToken=None, objects=[
        {"key": "datasets/imagenet/zz/last.bin", "relativePath": "zz/last.bin", "sizeBytes": 10, "ext": "bin"}])
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref", "paths": ["zz/last.bin"]}))
    assert body.get("confirmed") is False, body
    assert body["matched_count"] == 1 and body["size_bytes"] == 10
    assert _calls(fake, "browse_reference_objects")[0][2]["sub_prefix"] == "zz"


async def test_an_exact_path_past_the_pages_read_is_not_called_missing(mcp_client, fake, with_key, settings_fake):
    # The folder has more pages than the preview reads (the fake always hands a next page).
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref",
                                                                "paths": ["frames/img 2999.png"]}))
    assert body["code"] == "bucket_objects_unchecked", body
    assert body["unchecked"] == ["frames/img 2999.png"] and "missing" not in body
    assert "weren't found" not in body["message"] and "list fewer" not in body["next_step"]
    assert TAIL not in body["next_step"] and not _calls(fake, "fetch_reference_subset")


@pytest.mark.parametrize("sizes,phrase", [([70 * GIB, 70 * GIB, 70 * GIB], "200 GiB"), ([81 * GIB], "80 GiB")])
async def test_exact_paths_over_the_copy_limits_are_refused_in_the_preview(mcp_client, fake, with_key, settings_fake,
                                                                           sizes, phrase):
    names = [f"train/part {i}.bin" for i in range(len(sizes))]
    settings_fake["reference_listing"].update(nextContinuationToken=None, objects=[
        {"key": "datasets/imagenet/" + n, "relativePath": n, "sizeBytes": s, "ext": "bin"}
        for n, s in zip(names, sizes)])
    body = _payload(await mcp_client.call_tool("import_asset", {"source": "asset_ref", "paths": names}))
    assert body["code"] == "subset_not_fetchable", body
    assert phrase in body["message"]


# --- Registry login delete ------------------------------------------------------------------------------------------

async def test_registry_login_delete_names_the_pods_and_tasks(mcp_client, fake, with_key, monkeypatch):
    from meshive_mcp.tools import credentials

    async def usage(client, kind, credential_id, ws):
        return {"kind": kind, "label": "ghcr", "server": "ghcr.io", "templates": [{"template_id": 1, "name": "t"}],
                "active_pod_count": 2, "active_task_count": 1}

    monkeypatch.setattr(credentials, "credential_usage", usage)
    body = _payload(await mcp_client.call_tool("delete_credential", {"kind": "registry_login", "credential_id": 5,
                                                                     "workspace": WS}))
    assert body["confirmed"] is False
    assert "2 pods" in body["question"] and "stopped" in body["question"] and "1 unfinished task" in body["question"]


# --- Inference keys: lifetime and role ------------------------------------------------------------------------------

async def test_roll_key_preview_and_answer_say_the_lifetime(mcp_client, fake, with_key, state):
    prev = _payload(await mcp_client.call_tool("roll_inference_key", {"key_id": 3}))
    assert "does not expire" in prev["question"] and "Meshive API key used to make it" in prev["question"]
    body = _payload(await mcp_client.call_tool("roll_inference_key", {"key_id": 3, "confirm": True,
                                                                      "operation_id": prev["operation_id"]}))
    assert "does not expire" in body["next_step"] and "Meshive API key used to make it" in body["next_step"]
    state["keys"][0]["expiresAt"] = "2099-01-02T00:00:00Z"
    dated = _payload(await mcp_client.call_tool("roll_inference_key", {"key_id": 3}))
    assert "2099-01-02" in dated["question"] and "Meshive API key used to make it" in dated["question"]


async def test_create_key_answer_says_the_lifetime(mcp_client, fake, with_key, state):
    body = _payload(await mcp_client.call_tool("create_inference_key", {"confirm": True, "operation_id": OP}))
    assert "does not expire" in body["next_step"] and "Meshive API key used to make it" in body["next_step"]


@pytest.mark.parametrize("tool,args", [("create_inference_key", {}), ("roll_inference_key", {"key_id": 3})])
async def test_key_previews_refuse_a_viewer(mcp_client, fake, with_key, state, tool, args):
    fake["setup"] = lambda inst: setattr(inst, "workspaces", [Workspace.from_dict(
        {"namespaceName": WS, "workspaceName": "research", "memberRole": "viewer"})])
    body = _payload(await mcp_client.call_tool(tool, args))
    assert body["code"] == "forbidden", body
    assert "admin or billing" in body["next_step"]


# --- update_pod removing input assets -------------------------------------------------------------------------------

async def test_the_confirmed_edit_doesnt_send_the_agent_to_remove_pod_asset(mcp_client, fake, with_key):
    # The edit releases the asset's connection, so remove_pod_asset has nothing left to act on afterwards.
    args = {"pod": "pod-1", "remove_input_assets": [11]}
    prev = _payload(await mcp_client.call_tool("update_pod", args))
    assert "remove_pod_asset" in prev["question"]                    # before the edit it is still an option
    done = _payload(await mcp_client.call_tool("update_pod", {**args, "confirm": True, "operation_id": OP}))
    assert "remove_pod_asset" not in done["next_step"]
    assert "may stay in the pod" in done["next_step"] and "re-created again" in done["next_step"]
