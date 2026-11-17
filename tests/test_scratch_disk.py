"""The scratch disk at a template's workspace folder: short warnings in the pod previews, only when they apply, and
the error for a create no machine has disk for."""
import dataclasses
import json

import pytest
from meshive.exceptions import ConflictError, MeshiveAPIError
from meshive.models import Pod

pytestmark = pytest.mark.anyio
# The warnings work with any SDK (they are read from the server's answer); the scratch disk fields need one that
# reads them.
needs_scratch_fields = pytest.mark.skipif("scratch_disks" not in {f.name for f in dataclasses.fields(Pod)},
                                          reason="the installed meshive SDK doesn't read scratch disks yet")

WS = "0123456789abcdef"
POD = "0123456789abcdef-3"
SCRATCH = [{"path": "/workspace", "sizeGib": 20.4, "pricePerHour": "0.00208"}]
DELETES = {"code": "deletes_scratch_disk", "message": "Deletes the scratch disk at /workspace and its files."}
SPOT = {"code": "spot_scratch_erased", "message": "Spot: the scratch disk at /workspace is erased if the pod is reclaimed."}
HIDES = {"code": "hides_files", "message": "Files at /workspace/data on the scratch disk are hidden, not deleted."}
MOVE = {"code": "move_deletes_scratch_disk", "message": "Moving this pod deletes its scratch disk at /workspace and its files."}
CONSENT = " Obtain separate explicit consent and pass allow_data_loss=true."


def _payload(result):
    return json.loads(result.content[0].text)


def _setup(**attrs):
    return lambda inst: [setattr(inst, k, v) for k, v in attrs.items()]


# --- create_pod / estimate_pod ---------------------------------------------------------------------------------

@needs_scratch_fields
async def test_create_preview_carries_the_servers_warning_once(mcp_client, fake, with_key):
    fake["setup"] = _setup(estimate_warnings=[SPOT], estimate_scratch_disk={
        "paths": ["/workspace"], "newPaths": ["/workspace"], "initialGb": 20, "pricePerHourUsd": "0.00208"})
    prev = _payload(await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 457, "rental_type": "spot"}))
    assert prev["warnings"] == [SPOT]
    assert "warnings" not in prev["estimate"]                    # one list to show, at the top
    assert SPOT["message"] in prev["question"]
    # The scratch disk's amount is a number where prices already are, with the console's rounding.
    assert prev["estimate"]["scratch_disk"]["price_per_hour_display"] == "$0.002"
    assert prev["estimate"]["scratch_disk"]["new_paths"] == ["/workspace"]


async def test_previews_without_warnings_have_no_warnings_key(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 457}))
    assert "warnings" not in prev and "warnings" not in prev["estimate"]
    est = _payload(await mcp_client.call_tool("estimate_pod", {"name": "p", "template_id": 457}))
    assert "warnings" not in est
    edit = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "gpu_count": 2}))
    assert "warnings" not in edit and "scratch disk" not in edit["question"]


async def test_estimate_pod_shows_the_warning_with_the_price(mcp_client, fake, with_key):
    fake["setup"] = _setup(estimate_warnings=[SPOT])
    est = _payload(await mcp_client.call_tool("estimate_pod", {"name": "p", "template_id": 457, "rental_type": "spot"}))
    assert est["warnings"] == [SPOT]
    # next_step points at the list instead of splicing the warning between instructions to the agent.
    assert est["next_step"].startswith("Show the price per hour, hardware and each warnings message to the user.")
    assert SPOT["message"] not in est["next_step"]


# --- update_pod -----------------------------------------------------------------------------------------------

async def test_update_preview_lists_the_servers_warnings_most_severe_first(mcp_client, fake, with_key):
    fake["setup"] = _setup(estimate_warnings=[HIDES, SPOT, DELETES])
    prev = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "gpu_count": 2}))
    assert prev["warnings"] == [DELETES, SPOT, HIDES]
    assert prev["question"].endswith(f" {DELETES['message']} {SPOT['message']} {HIDES['message']}")
    assert prev["requires_data_loss_consent"] is False and prev["data_loss_warning"] == ""


async def test_update_that_may_move_the_pod_names_the_scratch_disk(mcp_client, fake, with_key):
    fake["setup"] = _setup(scratch_disks=SCRATCH, unpreserved_workspace=True)
    args = {"pod": POD, "gpu_count": 4, "allow_node_migration": True, "confirm": True, "operation_id": "edit-move-0100"}
    blocked = _payload(await mcp_client.call_tool("update_pod", args))
    assert blocked["confirmed"] is False and blocked["requires_data_loss_consent"] is True
    assert blocked["warnings"] == [MOVE] and blocked["data_loss_warning"] == MOVE["message"]
    assert blocked["question"].endswith(f" {MOVE['message']}{CONSENT}")
    assert not [c for c in fake["last"].calls if c[0] == "update_pod"]
    # The consent gate is unchanged: with allow_data_loss the edit goes through.
    done = _payload(await mcp_client.call_tool("update_pod", {**args, "allow_data_loss": True}))
    assert done["accepted"] is True


async def test_a_disk_the_edit_deletes_is_not_warned_about_twice(mcp_client, fake, with_key):
    fake["setup"] = _setup(scratch_disks=SCRATCH + [{"path": "/workspace/output", "sizeGib": 10}],
                           unpreserved_workspace=True, estimate_warnings=[DELETES])
    prev = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "allow_node_migration": True,
                                                              "gpu_count": 4}))
    moved = {"code": "move_deletes_scratch_disk",
             "message": "Moving this pod deletes its scratch disk at /workspace/output and its files."}
    assert prev["warnings"] == [DELETES, moved]
    assert prev["data_loss_warning"] == moved["message"]
    assert prev["question"].endswith(f" {DELETES['message']} {moved['message']}{CONSENT}")


async def test_a_move_after_an_edit_that_deletes_every_disk_needs_no_data_loss_consent(mcp_client, fake, with_key):
    """The edit itself deletes the only scratch disk (deletes_scratch_disk): the move deletes nothing more, so the
    preview asks for no separate consent and a confirmed call goes through without allow_data_loss."""
    fake["setup"] = _setup(scratch_disks=SCRATCH, unpreserved_workspace=True, estimate_warnings=[DELETES])
    args = {"pod": POD, "gpu_count": 4, "allow_node_migration": True}
    prev = _payload(await mcp_client.call_tool("update_pod", args))
    assert prev["warnings"] == [DELETES]
    assert prev["requires_data_loss_consent"] is False and prev["data_loss_warning"] == ""
    assert prev["question"].endswith(f" {DELETES['message']}") and CONSENT not in prev["question"]
    done = _payload(await mcp_client.call_tool("update_pod", {**args, "confirm": True,
                                                              "operation_id": "edit-move-0200"}))
    assert done["accepted"] is True
    sent = [c for c in fake["last"].calls if c[0] == "update_pod"]
    assert sent and sent[-1][2].get("allow_data_loss") is False


async def test_update_without_a_move_has_no_move_warning(mcp_client, fake, with_key):
    fake["setup"] = _setup(scratch_disks=SCRATCH, unpreserved_workspace=True)
    prev = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "gpu_count": 2}))
    assert "warnings" not in prev and "Moving this pod" not in prev["question"]


async def test_warnings_are_capped_dropping_hidden_files_first(mcp_client, fake, with_key):
    hides = [{"code": "hides_files", "message": f"Files at /workspace/d{i} on the scratch disk are hidden, not deleted."}
             for i in range(4)]
    fake["setup"] = _setup(scratch_disks=SCRATCH, unpreserved_workspace=True, estimate_warnings=[SPOT] + hides)
    prev = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "allow_node_migration": True,
                                                              "gpu_count": 4}))
    assert [w["code"] for w in prev["warnings"]] == ["move_deletes_scratch_disk", "spot_scratch_erased",
                                                     "hides_files", "hides_files", "hides_files"]


# --- start_pod ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("placement", ["any_node", "wait_any_node"])
async def test_start_that_moves_the_pod_names_its_scratch_disk(mcp_client, fake, with_key, placement):
    fake["setup"] = _setup(scratch_disks=SCRATCH, unpreserved_workspace=True)
    prev = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": placement}))
    assert prev["requires_data_loss_consent"] is True
    assert prev["warnings"] == [MOVE] and prev["data_loss_warning"] == MOVE["message"]
    assert prev["question"].endswith(f" {MOVE['message']}{CONSENT}")


@pytest.mark.parametrize("placement", ["any_node", "wait_any_node"])
async def test_start_that_moves_a_pod_with_no_listed_scratch_disk_still_says_what_is_lost(mcp_client, fake, with_key,
                                                                                         placement):
    """The server flags data at risk but lists no scratch disk (an old one waiting for cleanup): the consent question
    still says what the move deletes, without a path."""
    fake["setup"] = _setup(scratch_disks=[], unpreserved_workspace=True)
    prev = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": placement}))
    moved = {"code": "move_deletes_scratch_disk", "message": "Moving this pod deletes its scratch disk and its files."}
    assert prev["requires_data_loss_consent"] is True
    assert prev["warnings"] == [moved] and prev["data_loss_warning"] == moved["message"]
    assert prev["question"].endswith(f" {moved['message']}{CONSENT}")


async def test_long_paths_are_clipped_like_the_servers(mcp_client, fake, with_key):
    path = "/workspace/projects/team-alpha/run"                  # 34 characters
    fake["setup"] = _setup(scratch_disks=[{"path": path}], unpreserved_workspace=True)
    prev = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": "any_node"}))
    message = prev["warnings"][0]["message"]
    assert message == "Moving this pod deletes its scratch disk at …ace/projects/team-alpha/run and its files."
    assert len(message) <= 90


async def test_start_without_a_scratch_disk_or_a_move_has_no_warning(mcp_client, fake, with_key):
    fake["setup"] = _setup(scratch_disks=[], unpreserved_workspace=False)
    moving = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": "any_node"}))
    assert "warnings" not in moving and moving["requires_data_loss_consent"] is False
    fake["setup"] = _setup(scratch_disks=SCRATCH, unpreserved_workspace=True)
    staying = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": "same_node"}))
    assert "warnings" not in staying and staying["data_loss_warning"] == ""
    picked = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2"}))
    assert "warnings" not in picked and "scratch disk" not in picked["question"]


@pytest.mark.parametrize("tool,args", [
    ("update_pod", {"pod": POD, "gpu_count": 4}),
    ("update_pod", {"pod": POD, "gpu_count": 4, "allow_node_migration": True}),
    ("start_pod", {"pod": "pod-2"}),
    ("start_pod", {"pod": "pod-2", "placement": "any_node"}),
    ("start_pod", {"pod": "pod-2", "placement": "wait_any_node"}),
])
async def test_previews_of_a_pod_without_a_scratch_disk_never_mention_one(mcp_client, fake, with_key, tool, args):
    """Nothing in the preview — question, warnings, the server's estimate and its note — talks about a scratch disk."""
    fake["setup"] = _setup(scratch_disks=[], unpreserved_workspace=False)
    prev = _payload(await mcp_client.call_tool(tool, args))
    assert "scratch disk" not in json.dumps(prev)
    assert prev["requires_data_loss_consent"] in (False, None) and "warnings" not in prev


@pytest.mark.parametrize("tool,args", [
    ("update_pod", {"pod": POD, "gpu_count": 4, "allow_node_migration": True}),
    ("start_pod", {"pod": "pod-2", "placement": "any_node"}),
])
async def test_previews_of_a_move_say_once_that_it_deletes_the_scratch_disk(mcp_client, fake, with_key, tool, args):
    """The question carries the sentence once (the warning); the server's note doesn't repeat it."""
    fake["setup"] = _setup(scratch_disks=SCRATCH, unpreserved_workspace=True)
    prev = _payload(await mcp_client.call_tool(tool, args))
    assert prev["warnings"] == [MOVE]
    assert prev["question"].count("scratch disk") == 1
    assert "scratch disk" not in (prev.get("estimate") or {}).get("note", "")


@needs_scratch_fields
async def test_pod_rows_show_the_scratch_disk_price(mcp_client, fake, with_key):
    fake["setup"] = _setup(scratch_disks=SCRATCH, unpreserved_workspace=True)
    prev = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2"}))
    assert prev["pod"]["scratch_disks"] == [{"path": "/workspace", "size_gib": 20.4, "price_per_hour": "0.00208",
                                             "price_per_hour_display": "$0.002"}]


# --- errors -------------------------------------------------------------------------------------------------

async def test_create_refused_for_disk_says_what_to_ask(mcp_client, fake, with_key):
    message = "No machine has 20 GiB free for the scratch disk; try again later."
    fake["setup"] = lambda inst: inst.raise_on.update(create_pod=MeshiveAPIError(
        400, message, title="Not Enough Disk", raw={"detail": {"title": "Not Enough Disk", "message": message}}))
    result = await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 457, "confirm": True,
                                                       "operation_id": "create-disk-0001"})
    body = _payload(result)
    assert result.is_error and body["code"] == "not_enough_disk" and body["message"] == message
    assert body["next_step"].startswith("Nothing was created. Show the message to the user")


async def test_data_loss_refusal_passes_the_scratch_disk_paths_on(mcp_client, fake, with_key):
    message = ("Moving this pod deletes its scratch disk at /workspace and its files. Confirm the move with "
               "allow_data_loss.")
    detail = {"title": "Data Loss Consent Required", "message": message, "placement": "any_node",
              "hasUnpreservedWorkspace": True, "scratchDiskPaths": ["/workspace"]}
    fake["setup"] = lambda inst: inst.raise_on.update(start_pod=ConflictError(
        409, message, title=detail["title"], raw={"detail": detail}))
    body = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "confirm": True,
                                                             "operation_id": "start-move-0001"}))
    assert body["code"] == "data_loss_consent_required" and body["message"] == message
    assert body["scratchDiskPaths"] == ["/workspace"] and "scratch disk" in body["next_step"]
    assert "unpreserved" not in body["next_step"]


async def test_watched_folders_says_what_nested_volume_means(mcp_client):
    desc = {t.name: t.description for t in (await mcp_client.list_tools()).tools}["watched_folders"]
    assert "nested_volume = overlaps your volume" in desc
