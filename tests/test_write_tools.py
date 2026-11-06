"""Write tools — confirm gate, estimate/summary previews, SDK call arguments, fine-grained 409 mapping, logs."""
import json

import pytest
from meshive.exceptions import (AuthenticationError, ConflictError, InsufficientCreditError, MeshiveAPIError,
                                NotFoundError, PermissionDeniedError)
from meshive.models import WorkspaceDeleteCheck

pytestmark = pytest.mark.anyio

WS = "0123456789abcdef"


def _payload(result):
    return json.loads(result.content[0].text)


def _calls(fake, name):
    return [c for c in fake["last"].calls if c[0] == name]


async def test_create_pod_without_confirm_only_estimates(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 457, "gpu_model": "RTX 3060"}))
    assert body["confirmed"] is False and body["action"] == "create_pod"
    # The original number stays for math, and the string shown to people has 3 decimals like the console.
    assert body["estimate"]["price_per_hour"] == "0.068423"
    assert body["estimate"]["price_per_hour_display"] == "$0.068"
    assert "$0.068/hour" in body["question"]
    assert "confirm=true" in body["next_step"] and body["workspace"] == WS
    assert _calls(fake, "estimate_pod") and not _calls(fake, "create_pod")


async def test_create_pod_with_confirm_creates(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("create_pod", {
        "name": "p", "template_id": 457, "gpu_model": "RTX 3060", "gpu_count": 2, "rental_type": "spot",
        "volumes": [{"storage": "pv-a", "mount_path": "/data"}], "env": {"A": "1"}, "ports": [8888],
        "max_price_per_hour": 0.2, "confirm": True, "operation_id": "test-operation-0001"}))
    assert body["transaction_id"] == 14765 and body["pod_name"] is None and "Poll the pods tool" in body["next_step"]
    # A failed create drops off the transactions list after ~3 minutes — point the agent at the lookup by id.
    assert "usually runs within a minute" in body["next_step"]
    assert "transactions tool with transaction_id=14765" in body["next_step"]
    (_, args, kw), = _calls(fake, "create_pod")
    assert args == ("p", 457) and kw["workspace"] == WS and kw["gpu_count"] == 2 and kw["rental_type"] == "spot"
    assert kw["volumes"] == [{"storage": "pv-a", "mount_path": "/data"}] and kw["max_price_per_hour"] == 0.2
    assert "vcpu" not in kw   # None arguments are left to the SDK defaults


async def test_create_pod_passes_local_volumes_and_cpu_line_but_not_region(mcp_client, fake, with_key):
    local = [{"name": "scratch", "mount_path": "/scratch", "size_gb": 50, "encrypted": True}]
    args = {"name": "p", "template_id": 457, "local_volumes": local, "cpu_line": "amd-epyc-4-ddr5", "region": "KR"}
    prev = _payload(await mcp_client.call_tool("create_pod", args))
    (_, _, kw), = _calls(fake, "estimate_pod")
    assert kw["local_volumes"] == local and kw["cpu_line"] == "amd-epyc-4-ddr5"
    assert "region" not in kw                       # accepted for 1.x callers, ignored by the server, so never sent
    # The new local storage is part of the hourly total and keeps billing after the pod stops — say so in the question.
    assert "$0.082/hour" in prev["question"] and "$0.014/hour for the new local storage" in prev["question"]
    assert prev["estimate"]["breakdown_display"]["local_storage"] == "$0.014"

    _payload(await mcp_client.call_tool("create_pod", {**args, "confirm": True, "operation_id": prev["operation_id"]}))
    (_, _, kw), = _calls(fake, "create_pod")
    assert kw["local_volumes"] == local and kw["cpu_line"] == "amd-epyc-4-ddr5" and "region" not in kw


async def test_estimate_pod_mentions_local_storage_only_when_present(mcp_client, fake, with_key):
    plain = _payload(await mcp_client.call_tool("estimate_pod", {"name": "p", "template_id": 1, "region": "KR"}))
    assert "local storage" not in plain["next_step"] and "region" not in _calls(fake, "estimate_pod")[0][2]
    local = _payload(await mcp_client.call_tool("estimate_pod", {
        "name": "p", "template_id": 1, "local_volumes": [{"name": "s", "mount_path": "/s", "size_gb": 10}]}))
    assert "$0.014/hour for the new local storage" in local["next_step"]
    assert local["price_per_hour_display"] == "$0.082"


async def test_pod_tool_descriptions_cover_the_console_rules(mcp_client, fake, with_key):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("estimate_pod", "create_pod"):
        desc = tools[name].description
        assert "existing network storage" in desc and "new local storage" in desc, name
        assert "cpus tool" in desc and "8 cores / 32 GiB" in desc, name
        props = tools[name].input_schema["properties"]
        assert {"local_volumes", "cpu_line", "region", "volumes", "template_ports"} <= set(props), name
        assert "EXISTING network (nfs) storage" in props["volumes"]["description"], name
        local = props["local_volumes"]["description"]
        assert "NEW local storage" in local and "at least 10 GiB" in local and "spot" in local, name
        assert "Accepted for compatibility and ignored" in props["region"]["description"], name
        ports = props["template_ports"]["description"]
        assert "Changes the ports the template opens" in ports and "can't be removed" in ports, name


# The argument formats of estimate_pod and create_pod are in each argument's own description, where clients don't cut
# them; a phrase each one must carry.
POD_ARG_DESCRIPTIONS = {
    "workspace": "workspaces tool", "template_id": "templates tool", "gpu_model": "gpus tool",
    "cpu_line": "CPU pods only", "vcpu": "sizes the console offers", "ram_gb": "in GiB", "volumes": '"mount_path"',
    "local_volumes": '"size_gb"', "env": "leaves blank need a value", "secret_keys": "secret", "ports": '"external"',
    "template_ports": '"new_port"', "command": "32,000 characters", "region": "ignored",
    "max_price_per_hour": "caps the final COMPUTE hourly rate", "input_assets": '"strip_prefix": ""',
    "watched_folders": '"include_existing"', "watched_template_folders": "moves an output folder",
    "harvest_destination": '{"mode": "user_s3", "credential_id": n}', "model_set": "`selectable`",
    "hf_token_id": "accept the license on the page in `gated_sources`"}


async def test_pod_argument_formats_are_in_their_own_descriptions(mcp_client):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("estimate_pod", "create_pod"):
        props = tools[name].input_schema["properties"]
        for arg, phrase in POD_ARG_DESCRIPTIONS.items():
            assert phrase in props[arg].get("description", ""), (name, arg)
        assert "Each argument's format is in its own description" in tools[name].description, name
    # Both tools take the same arguments, described the same way.
    assert tools["estimate_pod"].input_schema["properties"] == {
        k: v for k, v in tools["create_pod"].input_schema["properties"].items() if k not in ("confirm", "operation_id")}


async def test_template_ports_are_passed_to_estimate_and_create(mcp_client, fake, with_key):
    changes = [{"port": 8188, "name": "ui", "new_port": 8189, "external": False}]
    est = _payload(await mcp_client.call_tool("estimate_pod", {"name": "p", "template_id": 457, "template_ports": changes}))
    assert est["price_per_hour"] == "0.068423"
    assert _calls(fake, "estimate_pod")[0][2]["template_ports"] == changes
    prev = _payload(await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 457, "template_ports": changes}))
    assert _calls(fake, "estimate_pod")[0][2]["template_ports"] == changes
    _payload(await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 457, "template_ports": changes,
                                                       "confirm": True, "operation_id": prev["operation_id"]}))
    assert _calls(fake, "create_pod")[0][2]["template_ports"] == changes
    # Left out when not given, so the SDK and server defaults apply.
    _payload(await mcp_client.call_tool("estimate_pod", {"name": "p", "template_id": 457}))
    assert "template_ports" not in _calls(fake, "estimate_pod")[0][2]


async def test_watched_template_folders_and_strip_prefix_reach_the_sdk(mcp_client, fake, with_key):
    """The console's create screen: template watched folders turned off, Keep structure for chosen asset files."""
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("estimate_pod", "create_pod"):
        props = tools[name].input_schema["properties"]
        assert "watched_template_folders" in props, name
        assets = props["input_assets"]["description"]
        assert '"strip_prefix": ""' in assets and "Only these keys" in assets, name
    folders = {"/outputs": {"enabled": False}}
    assets = [{"asset": "asset_b", "paths": ["unet/"], "strip_prefix": ""}]
    args = {"name": "p", "template_id": 457, "watched_template_folders": folders, "input_assets": assets}
    prev = _payload(await mcp_client.call_tool("create_pod", args))
    assert _calls(fake, "estimate_pod")[0][2]["watched_template_folders"] == folders
    _payload(await mcp_client.call_tool("create_pod", {**args, "confirm": True, "operation_id": prev["operation_id"]}))
    kw = _calls(fake, "create_pod")[0][2]
    assert kw["watched_template_folders"] == folders and kw["input_assets"] == assets


async def test_delete_pod_preview_then_delete(mcp_client, fake, with_key):
    preview = _payload(await mcp_client.call_tool("delete_pod", {"pod": "pod-3", "workspace": WS}))
    assert preview["confirmed"] is False and preview["pod"]["pod_name"] == "pod-3" and "cannot be undone" in preview["question"]
    assert not _calls(fake, "delete_pod")
    done = _payload(await mcp_client.call_tool("delete_pod", {"pod": "pod-3", "workspace": WS, "confirm": True, "operation_id": "test-operation-0001",
                                                              "delete_local_storages": ["pv-x"]}))
    assert done["action"] == "delete" and done["accepted"] is True
    (_, args, kw), = _calls(fake, "delete_pod")
    assert args == ("pod-3", WS) and kw["delete_local_storages"] == ["pv-x"]


async def test_lifecycle_tools(mcp_client, fake, with_key):
    for tool, method in (("stop_pod", "stop_pod"), ("restart_pod", "restart_pod")):
        body = _payload(await mcp_client.call_tool(tool, {"pod": "pod-1", "operation_id": "test-operation-0001"}))
        assert body["resource"] == "pod" and "asynchronously" in body["next_step"], tool
        (_, args, kw), = _calls(fake, method)
        assert args[:2] == ("pod-1", WS)


async def test_start_pod_needs_confirm_because_billing_resumes(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": "any_node"}))
    assert prev["confirmed"] is False and prev["action"] == "start_pod" and prev["pod"]["pod_name"] == "pod-2"
    assert "Billing resumes at $0.500/hour" in prev["question"] and "confirm=true" in prev["next_step"]
    assert prev["pod"]["price_per_hour_display"] == "$0.500"
    assert not _calls(fake, "start_pod")
    done = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": "any_node", "allow_data_loss": True, "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["resource"] == "pod" and "asynchronously" in done["next_step"]
    (_, args, kw), = _calls(fake, "start_pod")
    assert args[:2] == ("pod-2", WS) and kw["placement"] == "any_node"


@pytest.mark.parametrize("placement", ["wait_same_node", "wait_any_node"])
async def test_start_pod_wait_placements_are_passed_through(mcp_client, fake, with_key, placement):
    prev = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": placement}))
    assert prev["confirmed"] is False and prev["placement"] == placement and not _calls(fake, "start_pod")
    done = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": placement, "confirm": True,
                                                             "allow_data_loss": True, "operation_id": prev["operation_id"]}))
    assert "`waiting` until a machine has room" in done["next_step"]
    assert _calls(fake, "start_pod")[0][2]["placement"] == placement


async def test_start_pod_same_node_notes_cover_wait_same_node(mcp_client, fake, with_key):
    """spec_mismatch refuses both same-machine placements — waiting would never finish — so the preview says so for both."""
    fake["setup"] = lambda inst: setattr(inst, "same_node_reason", "spec_mismatch")
    for placement in ("same_node", "wait_same_node"):
        prev = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": placement}))
        assert "same_node and wait_same_node are refused" in prev["question"], placement
        assert "any_node or wait_any_node" in prev["question"], placement
    moving = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": "wait_any_node"}))
    assert "refused" not in moving["question"]

    fake["setup"] = lambda inst: setattr(inst, "same_node_reason", "node_unavailable")
    waiting = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": "wait_same_node"}))
    assert "can't take it now (node unavailable)" in waiting["question"] and "keeps it waiting" in waiting["question"]


async def test_start_pod_preview_hides_secrets_and_says_why_same_node_waits(mcp_client, fake, with_key):
    from conftest import POD_PASSWORD

    fake["setup"] = lambda inst: setattr(inst, "same_node_reason", "capacity")
    result = await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": "same_node"})
    assert POD_PASSWORD not in result.content[0].text                 # previews carry the whole Pod too
    prev = _payload(result)
    assert "cannot start on its original machine now (capacity)" in prev["question"]
    assert prev["data_loss_warning"] == ""


async def test_start_pod_without_placement_lets_the_server_pick(mcp_client, fake, with_key):
    """No placement: the server picks what the console would. The tool can't know in advance whether that moves the pod,
    so the preview says the server asks for consent itself, and allow_data_loss isn't sent without a known placement."""
    prev = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2"}))
    assert prev["confirmed"] is False and prev["placement"] is None and prev["requires_data_loss_consent"] is None
    assert "The server picks the placement the console would" in prev["question"]
    assert "data_loss_consent_required" in prev["data_loss_warning"] and prev["data_loss_warning"] in prev["question"]
    assert not _calls(fake, "start_pod")

    fake["setup"] = lambda inst: setattr(inst, "unpreserved_workspace", False)      # nothing to lose: no warning
    quiet = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "allow_data_loss": True}))
    assert quiet["data_loss_warning"] == "" and "permanently deleted" not in quiet["question"]
    assert "allow_data_loss is not sent without an explicit placement" in quiet["question"]

    # An older server doesn't report the placement it used.
    fake["setup"] = None
    done = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "confirm": True, "allow_data_loss": True,
                                                             "operation_id": prev["operation_id"]}))
    (_, args, kw), = _calls(fake, "start_pod")
    assert args[:2] == ("pod-2", WS) and kw["placement"] is None and kw["allow_data_loss"] is False
    assert done["placement"] is None and "picked" not in done["next_step"] and "asynchronously" in done["next_step"]


@pytest.mark.parametrize("picked,says", [("same_node", "asynchronously"), ("wait_same_node", "`waiting` until"),
                                         ("any_node", "another machine"), ("wait_any_node", "another machine")])
async def test_start_pod_reports_the_placement_the_server_picked(mcp_client, fake, with_key, picked, says):
    fake["setup"] = lambda inst: setattr(inst, "start_placement", picked)
    done = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "confirm": True,
                                                             "operation_id": "test-operation-0001"}))
    assert done["placement"] == picked and f"The server picked placement {picked}" in done["next_step"]
    assert says in done["next_step"]


async def test_start_pod_explicit_placement_is_reported_as_sent(mcp_client, fake, with_key):
    done = _payload(await mcp_client.call_tool("start_pod", {"pod": "pod-2", "placement": " Same_Node ", "confirm": True,
                                                             "operation_id": "test-operation-0001"}))
    assert done["placement"] == "same_node" and "picked" not in done["next_step"]
    assert _calls(fake, "start_pod")[0][2]["placement"] == "same_node"


async def test_defaulted_start_that_would_move_files_asks_for_consent(mcp_client, fake, with_key):
    """The server answers a start whose pick moves a pod with unpreserved files with 409 and the placement it picked;
    the agent is told to get consent and call again with that placement — not to repeat the same arguments."""
    detail = {"title": "Data Loss Consent Required", "message": "Moving this pod permanently deletes files.",
              "podName": "pod-2", "workspace": WS, "placement": "any_node", "hasUnpreservedWorkspace": True}
    fake["setup"] = lambda inst: inst.raise_on.update(start_pod=ConflictError(
        409, detail["message"], title=detail["title"], raw={"detail": detail}))
    result = await mcp_client.call_tool("start_pod", {"pod": "pod-2", "confirm": True,
                                                      "operation_id": "test-operation-0001"})
    body = _payload(result)
    assert result.is_error and body["code"] == "data_loss_consent_required" and body["placement"] == "any_node"
    assert body["operation_id"] == "test-operation-0001"
    step = body["next_step"]
    assert "Nothing was started" in step and "placement set to that `placement`" in step
    assert "allow_data_loss=true" in step and "same operation_id" in step and "explicit consent" in step
    assert "same arguments" not in step


async def test_storage_usage_warning_and_minimum_size(mcp_client, fake, with_key):
    desc = {t.name: t.description for t in (await mcp_client.list_tools()).tools}["create_storage"]
    assert "at least 10" in desc and "usage_warning_threshold" in desc
    args = {"name": "data", "size_gb": 20, "usage_warning": True, "usage_warning_threshold": 0.8}
    prev = _payload(await mcp_client.call_tool("create_storage", args))
    assert "A usage warning is sent when it is 80% full." in prev["question"]
    (_, _, kw), = _calls(fake, "estimate_storage")
    assert kw["usage_warning"] is True and kw["usage_warning_threshold"] == 0.8
    _payload(await mcp_client.call_tool("create_storage", {**args, "confirm": True, "operation_id": prev["operation_id"]}))
    (_, _, kw), = _calls(fake, "create_storage")
    assert kw["usage_warning"] is True and kw["usage_warning_threshold"] == 0.8
    default = _payload(await mcp_client.call_tool("create_storage", {"name": "data", "size_gb": 20, "usage_warning": True}))
    assert "90% full" in default["question"] and "usage_warning_threshold" not in _calls(fake, "estimate_storage")[0][2]


async def test_storage_tools(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("create_storage", {"name": "data", "size_gb": 10, "disk_type": "HDD",
                                                                  "region": "KR"}))
    assert prev["confirmed"] is False and prev["estimate"]["max_size_gb"] == 305
    assert "Create 10 GiB network (nfs) storage" in prev["question"]   # size_gb is GiB (capacity × 1024 MiB)
    (_, _, kw), = _calls(fake, "estimate_storage")
    # disk_type and region are accepted for 1.x callers but ignored by the server, so they are not sent.
    assert kw == {"workspace": WS, "storage_type": "nfs", "encrypted": False, "max_price_per_hour": None}
    done = _payload(await mcp_client.call_tool("create_storage", {"name": "data", "size_gb": 10, "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["transaction_id"] == 5 and "storages tool" in done["next_step"]
    prev = _payload(await mcp_client.call_tool("delete_storage", {"storage": "pv-1"}))
    assert prev["confirmed"] is False and prev["storage"]["linked_pods"] == ["p-1"] and "1 linked pod" in prev["question"]
    # total_size is MiB — appending GB as-is showed "(102400.0 GB, ...", inflated 1024 times.
    assert "(100 GiB, 1 linked pod(s))" in prev["question"] and prev["storage"]["total_size"] == 102400.0
    done = _payload(await mcp_client.call_tool("delete_storage", {"storage": "pv-1", "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["resource"] == "storage" and done["action"] == "delete"


async def test_local_storage_errors_say_where_local_storage_comes_from(mcp_client, fake, with_key):
    """Local storage is created with a pod now; the server's 400s come back with a next step that says so."""
    def detail(title, message):
        return MeshiveAPIError(400, message, title=title, raw={"detail": {"title": title, "message": message}})

    fake["setup"] = lambda inst: inst.raise_on.update(estimate_storage=detail(
        "Local Storage Not Supported", "Local storage can't be created on its own."))
    body = _payload(await mcp_client.call_tool("create_storage", {"name": "d", "size_gb": 10, "storage_type": "hostPath"}))
    assert body["code"] == "local_storage_not_supported" and "local_volumes" in body["next_step"]

    fake["setup"] = lambda inst: inst.raise_on.update(estimate_pod=detail(
        "Local Storage Not Attachable", "'pv-l' is local storage."))
    body = _payload(await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 1,
                                                              "volumes": [{"storage": "pv-l", "mount_path": "/d"}]}))
    assert body["code"] == "local_storage_not_attachable" and "local_volumes" in body["next_step"]


async def test_volume_size_follows_the_cli_rule():
    from meshive_mcp.tools.write_storages import _volume_size

    assert _volume_size(102400.0) == "100 GiB" and _volume_size(1536) == "1.5 GiB"
    assert _volume_size(1008) == "1008 MiB"          # a 1 GiB encrypted volume minus the LUKS header — the CLI writes MiB too
    assert _volume_size(float("nan")) == "size unknown" and _volume_size(None) == "size unknown"


async def test_serving_tools(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5, "price_cap_per_hour": 1.5,
                                                                  "max_replicas": 2}))
    assert prev["confirmed"] is False and prev["max_hourly_cost_usd"] == "3" and not _calls(fake, "deploy_serving")
    done = _payload(await mcp_client.call_tool("deploy_serving", {"model_registration_id": 5, "price_cap_per_hour": 1.5,
                                                                  "max_replicas": 2, "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["id"] == "42" and _calls(fake, "deploy_serving")[0][2]["price_cap_per_hour"] == 1.5
    # scale: widening the range needs confirm (billing goes up); shrinking or changing only autoscale/cap applies immediately
    up = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "max_replicas": 4}))
    assert up["confirmed"] is False and up["requested"] == {"max_replicas": 4}
    assert "cost goes up" in up["question"] and not _calls(fake, "scale_serving")
    body = _payload(await mcp_client.call_tool("scale_serving", {"serving": 42, "max_replicas": 4, "confirm": True, "operation_id": "test-operation-0001"}))
    assert body["action"] == "scale" and _calls(fake, "scale_serving")[0][2] == {"idempotency_key": "test-operation-0001", "min_replicas": None, "max_replicas": 4,
                                                                                    "autoscale": None, "price_cap_per_hour": None}
    # (fake makes a new client per tool call → _calls only sees the last call's record)
    down = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "max_replicas": 2}))
    assert down["action"] == "scale" and _calls(fake, "scale_serving")[0][2]["max_replicas"] == 2
    only_cap = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "price_cap_per_hour": 0.9}))
    assert only_cap["action"] == "scale" and _calls(fake, "scale_serving")[0][2]["price_cap_per_hour"] == 0.9
    assert _calls(fake, "get_serving")                # whether cost goes up is decided by comparing with the current state
    # pause is immediate, resume (paused=false) needs confirm
    _payload(await mcp_client.call_tool("pause_serving", {"operation_id": "test-operation-0001", "serving": 42, "paused": True}))
    assert _calls(fake, "pause_serving")[0][2]["paused"] is True
    resume = _payload(await mcp_client.call_tool("pause_serving", {"operation_id": "test-operation-0001", "serving": 42, "paused": False}))
    assert resume["confirmed"] is False and "Billing resumes" in resume["question"] and not _calls(fake, "pause_serving")
    _payload(await mcp_client.call_tool("pause_serving", {"serving": 42, "paused": False, "confirm": True, "operation_id": "test-operation-0001"}))
    assert _calls(fake, "pause_serving")[0][2]["paused"] is False
    prev = _payload(await mcp_client.call_tool("delete_serving", {"serving": 42}))
    assert prev["confirmed"] is False and prev["serving"]["model_name"] == "llama"


async def test_task_tools(mcp_client, fake, with_key):
    est = _payload(await mcp_client.call_tool("estimate_task", {"name": "t", "script": "print(1)", "image": "img",
                                                                "gpu_model": "RTX 3060"}))
    assert est["max_cost"] == "0.068423" and "submit_task" in est["next_step"]
    prev = _payload(await mcp_client.call_tool("submit_task", {"name": "t", "script": "print(1)", "image": "img",
                                                               "gpu_model": "RTX 3060"}))
    # max cost is a total cap, not hourly → 2 decimals like the console's other amounts.
    assert prev["confirmed"] is False and "up to $0.07" in prev["question"] and not _calls(fake, "submit_task")
    done = _payload(await mcp_client.call_tool("submit_task", {"name": "t", "script": "print(1)", "image": "img",
                                                               "gpu_model": "RTX 3060", "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["task"]["task_id"] == "task_1" and "logs with task=" in done["next_step"]
    assert "queued → scheduling → running" in done["next_step"]
    body = _payload(await mcp_client.call_tool("stop_task", {"operation_id": "test-operation-0001", "task": "task_1"}))
    assert body["resource"] == "task"


async def test_task_tools_pass_the_output_filter(mcp_client, fake, with_key):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("estimate_task", "submit_task"):
        assert "output_include" in tools[name].input_schema["properties"], name
        assert "relative to /outputs" in tools[name].description, name
        assert "null when no filter was given" in tools[name].description, name     # null alone isn't "ignored"
    patterns = ["*.safetensors", "samples/*.png"]
    args = {"name": "t", "script": "print(1)", "image": "img", "gpu_model": "RTX 3060", "output_include": patterns}
    _payload(await mcp_client.call_tool("estimate_task", args))
    assert _calls(fake, "estimate_task")[0][2]["output_include"] == patterns
    _payload(await mcp_client.call_tool("submit_task", args))       # the preview estimates with the same filter
    assert _calls(fake, "estimate_task")[0][2]["output_include"] == patterns and not _calls(fake, "submit_task")
    done = _payload(await mcp_client.call_tool("submit_task", {**args, "confirm": True,
                                                               "operation_id": "test-operation-0001"}))
    assert _calls(fake, "submit_task")[0][2]["output_include"] == patterns
    assert done["task"]["output_include"] == patterns               # the server's echo reaches the agent
    _payload(await mcp_client.call_tool("estimate_task", {"name": "t", "script": "print(1)", "image": "img",
                                                          "gpu_model": "RTX 3060"}))
    assert _calls(fake, "estimate_task")[0][2]["output_include"] is None     # omitted: every file


async def test_logs_tool(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("logs", {"pod": "pod-1", "tail": 50, "wait": 3}))
    assert body["text"] == "tick 1\ntick 2" and body["source"] == "live"
    assert _calls(fake, "get_pod_logs")[0][2] == {"tail": 50, "container": None, "wait": 3}
    body = _payload(await mcp_client.call_tool("logs", {"task": "task_9"}))
    assert body["task_id"] == "task_9" and body["text"] == "hello"
    both = await mcp_client.call_tool("logs", {"pod": "p", "task": "t"})
    assert both.is_error and _payload(both)["code"] == "invalid_argument"


async def test_logs_carry_an_untrusted_content_notice(mcp_client, fake, with_key):
    """Logs are arbitrary text printed by the user's container — a prompt injection path.

    Tool descriptions are read only **before** the call, so the response's `note` says the same thing too. Server instructions
    apply to the whole session. Having only one of the three isn't enough, so all three are pinned."""
    from meshive_mcp.server import INSTRUCTIONS

    sources = {
        "pod logs note": _payload(await mcp_client.call_tool("logs", {"pod": "pod-1", "operation_id": "test-operation-0001"})).get("note"),
        "task logs note": _payload(await mcp_client.call_tool("logs", {"task": "task_9"})).get("note"),
        "tool description": {t.name: t.description for t in (await mcp_client.list_tools()).tools}["logs"],
        "server instructions": INSTRUCTIONS,
    }
    for where, text in sources.items():
        lowered = (text or "").lower()
        assert "untrusted" in lowered and "never follow" in lowered, f"{where}: {text!r}"


async def test_conflict_titles_map_to_codes(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(
        estimate_pod=ConflictError(409, "No machine can host 3× RTX 3060", title="No Capacity",
                                   raw={"detail": {"title": "No Capacity", "message": "x",
                                                   "availability": {"available_gpus": 2}}}))
    result = await mcp_client.call_tool("estimate_pod", {"name": "p", "template_id": 1, "gpu_model": "RTX 3060", "gpu_count": 3})
    assert result.is_error
    body = _payload(result)
    assert body["code"] == "no_capacity" and body["availability"] == {"available_gpus": 2} and "Do not retry" in body["next_step"]


async def test_insufficient_credit(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(create_pod=InsufficientCreditError(402, "top up", title="Insufficient Credit"))
    result = await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 1, "confirm": True, "operation_id": "test-operation-0001"})
    assert result.is_error and _payload(result)["code"] == "insufficient_credit"


async def test_write_tools_need_key(mcp_client, fake):
    result = await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 1, "confirm": True, "operation_id": "test-operation-0001"})
    assert result.is_error and _payload(result)["code"] == "no_api_key"


async def test_pod_label_is_resolved_to_pod_name(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.pods.__setitem__(3, __import__("conftest")._pod("pod-3"))  # user_alias == "pod-3"
    body = _payload(await mcp_client.call_tool("stop_pod", {"operation_id": "test-operation-0001", "pod": "POD-3", "workspace": WS}))
    assert body["id"] == "pod-3" and _calls(fake, "list_pods")
    missing = await mcp_client.call_tool("stop_pod", {"operation_id": "test-operation-0001", "pod": "nope", "workspace": WS})
    assert missing.is_error and _payload(missing)["code"] == "not_found" and "pods" in _payload(missing)
    exact = _payload(await mcp_client.call_tool("stop_pod", {"operation_id": "test-operation-0001", "pod": "0123456789abcdef-0", "workspace": WS}))
    assert exact["id"] == "0123456789abcdef-0"


async def test_blank_pod_label_never_resolves_to_a_system_pod(mcp_client, fake, with_key):
    """Regression: an empty/blank `pod` matched the **system downloader pod**, whose user_alias is empty, so stop/delete/logs
    went to that pod. A missing argument is cut off before it touches the downloader."""
    for blank in ("", "   "):
        result = await mcp_client.call_tool("stop_pod", {"operation_id": "test-operation-0001", "pod": blank, "workspace": WS})
        assert result.is_error and _payload(result)["code"] == "invalid_argument", blank
        assert not _calls(fake, "stop_pod"), blank

    # Downloaders are left out of label matching too — their label is empty, so they must not come up as candidates.
    missing = await mcp_client.call_tool("stop_pod", {"operation_id": "test-operation-0001", "pod": "dl-0-label", "workspace": WS})
    body = _payload(missing)
    assert missing.is_error and body["code"] == "not_found"
    assert all(not str(c["pod_name"]).startswith("dl-") for c in body["pods"]), body["pods"]


async def test_name_taken_tells_agent_to_check_the_list_first(mcp_client, fake, with_key):
    """When retrying a create whose response was lost comes back as 409 Name Taken, check the list first instead of "use another name"."""
    fake["setup"] = lambda inst: inst.raise_on.update(
        create_pod=ConflictError(409, "A pod named 'p' already exists in this workspace.", title="Name Taken"))
    result = await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 1, "confirm": True, "operation_id": "test-operation-0001"})
    body = _payload(result)
    assert result.is_error and body["code"] == "name_taken"
    assert "list tool" in body["next_step"] and "before creating anything else" in body["next_step"]


async def test_creation_failed_pod_gets_no_retry_advice(mcp_client, fake, with_key):
    """A pod cleaned up after a failed create is final — the generic "retry with the same operation_id" line is left off."""
    fake["setup"] = lambda inst: inst.raise_on.update(
        delete_pod=NotFoundError(404, "Pod 'x-0' failed to start and was cleaned up: CrashLoopBackOff",
                                 title="Pod Creation Failed"))
    result = await mcp_client.call_tool("delete_pod", {"pod": "0123456789abcdef-0", "workspace": WS, "confirm": True,
                                                       "operation_id": "test-operation-0001"})
    body = _payload(result)
    assert result.is_error and body["code"] == "pod_creation_failed" and body["operation_id"] == "test-operation-0001"
    assert "Don't retry this pod" in body["next_step"] and "Any retry MUST" not in body["next_step"]


async def test_scale_serving_confirms_cap_raise_and_autoscale_on(mcp_client, fake, with_key):
    """Not just the replica range — raising the cap and turning on autoscale can raise cost too; the same confirm gate."""
    from conftest import Serving
    capped = Serving.from_dict({"id": 42, "namespaceName": WS, "modelName": "llama", "framework": "vllm", "status": "active",
                                "currentReplicas": 2, "minReplicas": 1, "maxReplicas": 3, "autoScaleEnabled": False,
                                "priceCapPerHour": "1.5"})
    async def get_serving(self, sid):
        self._rec("get_serving", sid); return capped
    fake["setup"] = lambda inst: setattr(inst, "get_serving", get_serving.__get__(inst))
    raise_cap = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "price_cap_per_hour": 2}))
    assert raise_cap["confirmed"] is False and raise_cap["requested"] == {"price_cap_per_hour": 2}
    assert "$2.000/hour per replica" in raise_cap["question"] and raise_cap["serving"]["price_cap_per_hour_display"] == "$1.500"
    assert not _calls(fake, "scale_serving")
    auto_on = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "autoscale": True}))
    assert auto_on["confirmed"] is False and "autoscale on" in auto_on["question"] and not _calls(fake, "scale_serving")
    lower = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "price_cap_per_hour": 1, "autoscale": False}))
    assert lower["action"] == "scale" and _calls(fake, "scale_serving")[0][2]["price_cap_per_hour"] == 1
    done = _payload(await mcp_client.call_tool("scale_serving", {"operation_id": "test-operation-0001", "serving": 42, "price_cap_per_hour": 2, "confirm": True}))
    assert done["action"] == "scale" and _calls(fake, "scale_serving")[0][2]["price_cap_per_hour"] == 2


async def test_logs_cursor_reads_external_task_increments(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("logs", {"task": "task_9", "cursor": 12}))
    assert body["task_id"] == "task_9" and _calls(fake, "get_task_logs")[0][2] == {"tail": 200, "wait": None, "cursor": 12}
    body = _payload(await mcp_client.call_tool("logs", {"task": "task_9"}))
    assert _calls(fake, "get_task_logs")[0][2]["cursor"] is None            # omitted = last tail lines
    bad = await mcp_client.call_tool("logs", {"pod": "pod-1", "cursor": 3})
    assert bad.is_error and _payload(bad)["code"] == "invalid_argument"


async def test_pod_tools_no_longer_take_disk_gb(mcp_client, fake, with_key):
    """The system disk is fixed by a server-side formula — the argument that pretended to take it is removed from the schema."""
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("estimate_pod", "create_pod"):
        assert "disk_gb" not in tools[name].input_schema["properties"], name
    # If the model sends it anyway, MCP drops arguments outside the schema — it's not in the SDK signature either, so it can't reach the server.
    _payload(await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 1, "disk_gb": 30, "confirm": True,
                                                       "operation_id": "test-operation-0001"}))
    assert "disk_gb" not in _calls(fake, "create_pod")[0][2]


async def test_model_registration_tools(mcp_client, fake, with_key):
    listed = _payload(await mcp_client.call_tool("models", {}))
    assert listed["items"][0]["registration_id"] == 12
    creds = _payload(await mcp_client.call_tool("source_credentials", {}))
    assert [t["label"] for t in creds["hf_tokens"]] == ["hf-main"] and [k["token_id"] for k in creds["civitai_keys"]] == [9]
    detected = _payload(await mcp_client.call_tool("detect_model", {"huggingface_repo": "Qwen/Qwen3-0.6B", "hf_token_id": 4}))
    assert detected["status"] == "ok" and detected["context_length"] == 40960
    assert _calls(fake, "detect_model")[0][2] == {"workspace": WS, "hf_token_id": 4}

    # No cost, so it registers without confirm, but operation_id still goes as the idempotency key per the write convention.
    done = _payload(await mcp_client.call_tool("register_model", {"huggingface_repo": "Qwen/Qwen3-0.6B",
                                                                   "framework": "vllm",
                                                                   "operation_id": "test-operation-0002"}))
    assert done["registration_id"] == 12 and "deploy_serving" in done["next_step"]
    (_, args, kw), = _calls(fake, "register_model")
    assert args == ("Qwen/Qwen3-0.6B",) and kw["framework"] == "vllm" and kw["idempotency_key"] == "test-operation-0002"

    prev = _payload(await mcp_client.call_tool("delete_model", {"registration_id": 12}))
    assert prev["confirmed"] is False and prev["model"]["name"] == "qwen-small" and not _calls(fake, "delete_model")
    missing = await mcp_client.call_tool("delete_model", {"registration_id": 99})
    assert missing.is_error and "not in workspace" in missing.content[0].text
    gone = _payload(await mcp_client.call_tool("delete_model", {"registration_id": 12, "confirm": True,
                                                                 "operation_id": prev["operation_id"]}))
    assert gone["action"] == "delete" and _calls(fake, "delete_model")[0][1][0] == "12"


async def test_register_model_passes_on_why_sglang_is_refused(mcp_client, fake, with_key):
    """Text models run on vLLM. The server refuses sglang with a 400 that says so, and the agent gets that reason."""
    message = "SGLang is no longer offered for text models. Text models are served with vLLM — omit framework or set framework='vllm'."
    fake["setup"] = lambda inst: inst.raise_on.update(register_model=MeshiveAPIError(
        400, message, title="SGLang Not Offered", raw={"detail": {"title": "SGLang Not Offered", "message": message}}))
    body = _payload(await mcp_client.call_tool("register_model", {"huggingface_repo": "Qwen/Qwen3-0.6B",
                                                                   "framework": "sglang",
                                                                   "operation_id": "test-operation-0003"}))
    assert body["code"] == "invalid_request" and "vLLM" in body["message"]
    (_, _, kw), = _calls(fake, "register_model")
    assert kw["framework"] == "sglang"


async def test_import_asset_is_a_write_without_confirm(mcp_client, fake, with_key):
    refused = await mcp_client.call_tool("import_asset", {"source": "Qwen/Qwen3-0.6B"})
    assert refused.is_error and "operation_id_required" in refused.content[0].text and not _calls(fake, "import_asset")
    done = _payload(await mcp_client.call_tool("import_asset", {"source": "Qwen/Qwen3-0.6B", "paths": ["*.safetensors"],
                                                                 "operation_id": "test-operation-0003"}))
    assert done["asset_id"] == "asset_new" and "input_assets" in done["next_step"]
    (_, args, kw), = _calls(fake, "import_asset")
    assert args == ("Qwen/Qwen3-0.6B",) and kw["paths"] == ["*.safetensors"] and kw["idempotency_key"] == "test-operation-0003"


async def test_create_pod_passes_assets_and_watched_folders(mcp_client, fake, with_key):
    args = {"name": "p", "template_id": 457, "gpu_model": "RTX 3060", "input_assets": ["asset_a"],
            "watched_folders": [{"path": "/workspace/results", "include": ["*.csv"]}]}
    prev = _payload(await mcp_client.call_tool("create_pod", args))
    assert prev["confirmed"] is False
    (_, _, kw), = _calls(fake, "estimate_pod")
    assert kw["input_assets"] == ["asset_a"] and kw["watched_folders"] == [{"path": "/workspace/results", "include": ["*.csv"]}]


async def test_watched_folders_read_and_confirm_only_when_growing(mcp_client, fake, with_key):
    seen = _payload(await mcp_client.call_tool("watched_folders", {"pod": "pod-1"}))
    assert seen["revision"] == 4 and [f["path"] for f in seen["folders"]] == ["/workspace/outputs", "/workspace/logs"]

    grow = {"pod": "pod-1", "expected_version": 4, "template": {"/workspace/outputs": {"enabled": True}},
            "user": [{"path": "/workspace/logs", "include": ["*.txt"]}, {"path": "/workspace/ckpt"}]}
    prev = _payload(await mcp_client.call_tool("set_watched_folders", grow))
    assert prev["confirmed"] is False and prev["adds_or_enables"] == ["/workspace/ckpt"]
    assert not _calls(fake, "set_watched_folders")
    done = _payload(await mcp_client.call_tool("set_watched_folders", {**grow, "confirm": True,
                                                                        "operation_id": prev["operation_id"]}))
    assert done["revision"] == 5
    (_, args, kw), = _calls(fake, "set_watched_folders")
    assert args == ("pod-1", WS) and kw["expected_version"] == 4 and kw["idempotency_key"] == prev["operation_id"]

    shrink = {"pod": "pod-1", "expected_version": 5, "template": {"/workspace/outputs": {"enabled": False}}, "user": [],
              "operation_id": "test-operation-0004"}
    assert _payload(await mcp_client.call_tool("set_watched_folders", shrink))["revision"] == 5
    (_, _, kw), = _calls(fake, "set_watched_folders")          # reductions are written right away without confirmation
    assert kw["expected_version"] == 5


async def test_ssh_access_returns_command_and_password_but_not_the_web_url(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("ssh_access", {"pod": "pod-1"})
    body = _payload(result)
    assert body["command"] == "ssh -p 2222 root@m.example" and body["password"] == "pw-123"
    assert body["expires_at"].startswith("2030-") and "only to the user" in body["note"]
    assert "cHctMTIz" not in result.content[0].text and "web_url" not in body


async def test_create_workspace_without_confirm_only_checks_the_name(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("create_workspace", {"name": "research-2"}))
    assert body["confirmed"] is False and body["action"] == "create_workspace" and body["name"] == "research-2"
    assert "'research-2'" in body["question"] and "costs nothing by itself" in body["question"]
    assert "billed as usual" in body["question"] and "confirm=true" in body["next_step"]
    assert body["estimate"]["name"] == "research-2" and body["estimate"]["price_per_hour"] == "0"
    assert "raw" not in body["estimate"]
    # The preview is a read: nothing is created, and the read call carries no Idempotency-Key.
    assert _calls(fake, "estimate_workspace") == [("estimate_workspace", ("research-2",), {})]
    assert not _calls(fake, "create_workspace")
    # The preview's operation_id is the one the confirmation reuses.
    assert body["operation_id"] and "Reuse this operation_id" in body["next_step"]


async def test_create_workspace_with_confirm_creates_and_says_to_wait_for_running(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("create_workspace", {"name": "research-2"}))
    done = _payload(await mcp_client.call_tool("create_workspace", {"name": "research-2", "confirm": True,
                                                                     "operation_id": prev["operation_id"]}))
    assert done["workspace"] == "fedcba9876543210" and done["name"] == "research-2" and done["transaction_id"] == 21
    assert done["estimate"]["price_per_hour"] == "0" and done["operation_id"] == prev["operation_id"]
    step = done["next_step"]
    assert "`fedcba9876543210`" in step and "transaction" not in step  # the transactions tool can't look it up
    assert "`workspaces` tool with no arguments until its status is `running`" in step
    assert "before creating anything in it" in step and "setup failed: tell the user" in step
    assert "taking longer than usual and don't create another" in step
    (_, args, kw), = _calls(fake, "create_workspace")
    assert args == ("research-2",) and kw == {"idempotency_key": prev["operation_id"]}
    assert not _calls(fake, "estimate_workspace")                 # the confirmed call does not estimate again

    again = _payload(await mcp_client.call_tool("create_workspace", {"name": "research-2", "confirm": True,
                                                                      "operation_id": prev["operation_id"]}))
    assert again["operation_id"] == prev["operation_id"]        # a retry reuses the key, so the server can replay
    assert [c[2] for c in _calls(fake, "create_workspace")] == [{"idempotency_key": prev["operation_id"]}]


async def test_create_workspace_confirm_needs_an_operation_id(mcp_client, fake, with_key):
    refused = await mcp_client.call_tool("create_workspace", {"name": "research-2", "confirm": True})
    assert refused.is_error and _payload(refused)["code"] == "operation_id_required"
    assert not _calls(fake, "create_workspace")


async def test_create_workspace_strips_the_name_and_generates_one_when_blank(mcp_client, fake, with_key):
    _payload(await mcp_client.call_tool("create_workspace", {"name": "  research-2  "}))
    assert _calls(fake, "estimate_workspace")[0][1] == ("research-2",)

    for blank in (None, "", "   "):
        args = {} if blank is None else {"name": blank}
        prev = _payload(await mcp_client.call_tool("create_workspace", args))
        assert prev["name"] is None and "generated name" in prev["question"], blank
        assert "costs nothing by itself" in prev["question"], blank
        assert _calls(fake, "estimate_workspace")[-1][1] == (None,), blank
        done = _payload(await mcp_client.call_tool("create_workspace", {**args, "confirm": True,
                                                                         "operation_id": prev["operation_id"]}))
        assert done["name"] == "my-workspace-ab12" and "'my-workspace-ab12'" in done["next_step"], blank
        assert _calls(fake, "create_workspace")[-1][1] == (None,), blank


async def test_create_workspace_tool_description(mcp_client):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    tool = tools["create_workspace"]
    assert tool.title == "Create Workspace"
    assert tool.annotations.read_only_hint is False and tool.annotations.destructive_hint is False
    assert "costs nothing" in tool.description and "63 characters" in tool.description
    assert "`creating` until it is `running`" in tool.description and "workspaces tool" in tool.description
    assert {"name", "confirm", "operation_id"} <= set(tool.input_schema["properties"])
    assert "workspace" not in tool.input_schema["properties"]
    assert "create_workspace" in tools["workspaces"].description


async def test_create_workspace_name_taken_keeps_the_operation_id(mcp_client, fake, with_key):
    taken = ConflictError(409, "You already have a workspace named 'research'. Choose a different name.",
                          title="Name Taken")
    fake["setup"] = lambda inst: inst.raise_on.update(estimate_workspace=taken)
    result = await mcp_client.call_tool("create_workspace", {"name": "research"})
    body = _payload(result)
    assert result.is_error and body["code"] == "name_taken" and "'research'" in body["message"]
    assert "list tool" in body["next_step"] and "pick another name" in body["next_step"]
    assert not _calls(fake, "create_workspace")

    # A confirmed create refused for the same reason keeps its operation_id, like every other write.
    fake["setup"] = lambda inst: inst.raise_on.update(create_workspace=taken)
    result = await mcp_client.call_tool("create_workspace", {"name": "research", "confirm": True,
                                                              "operation_id": "test-operation-0001"})
    body = _payload(result)
    assert result.is_error and body["code"] == "name_taken" and body["operation_id"] == "test-operation-0001"
    assert "Any retry MUST" not in body["next_step"]                  # it says to pick another name, not the same arguments


async def test_create_workspace_without_a_cluster_asks_to_wait(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(
        create_workspace=MeshiveAPIError(503, "No cluster can host a new workspace right now.",
                                         title="No Cluster Available"))
    result = await mcp_client.call_tool("create_workspace", {"name": "research-2", "confirm": True,
                                                              "operation_id": "test-operation-0001"})
    body = _payload(result)
    assert result.is_error and body["code"] == "temporarily_unavailable" and body["retry_after"] == 30
    assert body["operation_id"] == "test-operation-0001" and "MUST reuse this operation_id" in body["next_step"]


async def test_create_workspace_needs_a_write_key_but_the_preview_does_not(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(
        create_workspace=PermissionDeniedError(403, "API key lacks required scope: write"))
    prev = _payload(await mcp_client.call_tool("create_workspace", {"name": "research-2"}))
    assert prev["confirmed"] is False                       # only estimate_workspace (read scope) was called
    result = await mcp_client.call_tool("create_workspace", {"name": "research-2", "confirm": True,
                                                              "operation_id": prev["operation_id"]})
    assert result.is_error and _payload(result)["code"] == "write_scope_required"


async def test_create_workspace_needs_a_key(mcp_client, fake):
    for args in ({}, {"confirm": True, "operation_id": "test-operation-0001"}):
        result = await mcp_client.call_tool("create_workspace", args)
        assert result.is_error and _payload(result)["code"] == "no_api_key"
    assert "last" not in fake


# --- update_workspace, delete_workspace ---

RENAME = {"workspace": WS, "name": "lab-2"}
OP = "test-operation-0001"
_BOTH_TOOLS = [("update_workspace", {"name": "lab-2"}), ("delete_workspace", {})]


async def test_update_workspace_without_confirm_only_checks_the_name(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("update_workspace", RENAME))
    assert body["confirmed"] is False and body["action"] == "update_workspace"
    assert body["workspace"] == WS and body["current_name"] == "research" and body["name"] == "lab-2"
    assert "'research' to 'lab-2'" in body["question"] and "Other members see the new name" in body["question"]
    assert body["operation_id"] and "Reuse this operation_id" in body["next_step"]
    assert _calls(fake, "estimate_workspace_update") == [("estimate_workspace_update", (WS,), {"name": "lab-2"})]
    assert not _calls(fake, "update_workspace") and not _calls(fake, "list_workspaces")  # an id needs no lookup


async def test_update_workspace_with_confirm_renames_and_says_to_keep_using_the_id(mcp_client, fake, with_key):
    done = _payload(await mcp_client.call_tool("update_workspace", {**RENAME, "confirm": True, "operation_id": OP}))
    assert done["workspace"] == WS and done["name"] == "lab-2" and done["operation_id"] == OP
    assert f"`{WS}`" in done["next_step"] and "Keep using the id" in done["next_step"]
    assert "can take a moment to show the new name" in done["next_step"]
    assert _calls(fake, "update_workspace") == [("update_workspace", (WS,), {"name": "lab-2", "idempotency_key": OP})]
    assert not _calls(fake, "estimate_workspace_update")           # the confirmed call does not estimate again


async def test_update_workspace_name_is_required_and_stripped(mcp_client, fake, with_key):
    _payload(await mcp_client.call_tool("update_workspace", {"workspace": WS, "name": "  lab-2  "}))
    assert _calls(fake, "estimate_workspace_update")[0][2] == {"name": "lab-2"}

    fake.clear()
    for confirm in (False, True):
        result = await mcp_client.call_tool("update_workspace", {"workspace": WS, "name": "   ", "confirm": confirm,
                                                                 "operation_id": OP})
        body = _payload(result)
        assert result.is_error and body["code"] == "invalid_argument" and "`name`" in body["message"], confirm
    assert "last" not in fake                                      # refused before any client was built


async def test_update_workspace_name_taken_keeps_the_operation_id(mcp_client, fake, with_key):
    taken = ConflictError(409, "You already have a workspace named 'lab'.", title="Name Taken")
    fake["setup"] = lambda inst: inst.raise_on.update(estimate_workspace_update=taken)
    result = await mcp_client.call_tool("update_workspace", {"workspace": WS, "name": "lab"})
    body = _payload(result)
    assert result.is_error and body["code"] == "name_taken" and "update_workspace" in body["next_step"]
    assert not _calls(fake, "update_workspace")

    fake["setup"] = lambda inst: inst.raise_on.update(update_workspace=taken)
    result = await mcp_client.call_tool("update_workspace", {"workspace": WS, "name": "lab", "confirm": True,
                                                             "operation_id": OP})
    body = _payload(result)
    assert result.is_error and body["code"] == "name_taken" and body["operation_id"] == OP
    assert "Any retry MUST" not in body["next_step"]               # it says to ask for another name, not the same arguments


async def test_delete_workspace_without_confirm_checks_and_deletes_nothing(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("delete_workspace", {"workspace": WS}))
    assert body["confirmed"] is False and body["action"] == "delete_workspace" and body["workspace"] == WS
    assert body["deletable"] is True and body["blockers"] == {}
    assert "'research'" in body["question"] and f"`{WS}`" in body["question"] and "can't be undone" in body["question"]
    assert body["operation_id"] and "Reuse this operation_id" in body["next_step"]
    assert _calls(fake, "check_workspace_delete") == [("check_workspace_delete", (WS,), {})]
    assert not _calls(fake, "delete_workspace")


async def test_delete_workspace_preview_of_a_workspace_with_things_left_is_refused(mcp_client, fake, with_key):
    blockers = {"assets": 3, "pods": 1, "task_outputs": 1}
    fake["setup"] = lambda inst: setattr(inst, "workspace_blockers", blockers)
    result = await mcp_client.call_tool("delete_workspace", {"workspace": WS})
    body = _payload(result)
    assert result.is_error and body["code"] == "workspace_not_empty" and body["blockers"] == blockers
    assert "confirmed" not in body and "question" not in body      # nothing to agree to
    assert "'research'" in body["message"] and "only if the user asks" in body["next_step"]
    assert "A person can still delete it in the console" in body["next_step"]
    assert "Any retry MUST" not in body["next_step"]               # it says what to do for each blocker instead
    assert not _calls(fake, "delete_workspace")


async def test_delete_workspace_treats_a_check_without_an_answer_as_blocked(mcp_client, fake, with_key):
    def unanswered(inst):
        async def check(workspace):
            return WorkspaceDeleteCheck.from_dict({"workspace": workspace, "name": "research"})
        inst.check_workspace_delete = check
    fake["setup"] = unanswered
    result = await mcp_client.call_tool("delete_workspace", {"workspace": WS})
    assert result.is_error and _payload(result)["code"] == "workspace_not_empty"


async def test_delete_workspace_with_confirm_deletes_and_a_retry_reuses_the_key(mcp_client, fake, with_key):
    done = _payload(await mcp_client.call_tool("delete_workspace", {"workspace": WS, "confirm": True, "operation_id": OP}))
    assert done["resource"] == "workspace" and done["id"] == WS and done["action"] == "delete"
    assert done["operation_id"] == OP
    step = done["next_step"]
    assert "Deletion was accepted" in step and "leaves the workspaces list within a moment" in step
    assert "the delete didn't fail, so don't delete it again" in step and "name stays taken for a while" in step
    assert "right away" not in step and "for a moment" not in step
    assert _calls(fake, "delete_workspace") == [("delete_workspace", (WS,), {"idempotency_key": OP})]
    assert not _calls(fake, "check_workspace_delete")              # the server checks again itself

    # The workspace is gone from the list by now; a retry by id doesn't depend on it.
    fake["setup"] = lambda inst: setattr(inst, "workspaces", [])
    again = _payload(await mcp_client.call_tool("delete_workspace", {"workspace": WS, "confirm": True, "operation_id": OP}))
    assert again["operation_id"] == OP and _calls(fake, "delete_workspace")[0][2] == {"idempotency_key": OP}
    assert not _calls(fake, "list_workspaces")


async def test_delete_workspace_refused_by_the_server_after_a_clean_preview(mcp_client, fake, with_key):
    blockers = {"pods": 1}
    fake["setup"] = lambda inst: inst.raise_on.update(delete_workspace=ConflictError(
        409, "'research' still holds: pods (1).", title="Workspace Not Empty",
        raw={"detail": {"title": "Workspace Not Empty", "message": "'research' still holds: pods (1).",
                        "blockers": blockers}}))
    result = await mcp_client.call_tool("delete_workspace", {"workspace": WS, "confirm": True, "operation_id": OP})
    body = _payload(result)
    assert result.is_error and body["code"] == "workspace_not_empty" and body["blockers"] == blockers
    assert body["operation_id"] == OP and "only if the user asks" in body["next_step"]
    assert "without confirm" in body["next_step"]                  # not "the same arguments", which are confirm=true
    assert "Any retry MUST" not in body["next_step"]


async def test_delete_workspace_refused_as_not_deletable_has_its_own_retry_advice(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(delete_workspace=MeshiveAPIError(
        400, "This workspace is protected and cannot be deleted.", title="Unable to delete workspace"))
    result = await mcp_client.call_tool("delete_workspace", {"workspace": WS, "confirm": True, "operation_id": OP})
    body = _payload(result)
    assert result.is_error and body["code"] == "workspace_not_deletable" and body["operation_id"] == OP
    assert "don't retry" in body["next_step"] and "Any retry MUST" not in body["next_step"]


async def test_delete_workspace_has_no_way_to_force_it(mcp_client):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    tool = tools["delete_workspace"]
    assert set(tool.input_schema["properties"]) == {"workspace", "confirm", "operation_id"}
    assert "force" not in tool.description.lower()


async def test_workspace_tool_descriptions(mcp_client):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    update, delete = tools["update_workspace"], tools["delete_workspace"]
    assert update.title == "Rename Workspace" and delete.title == "Delete Workspace"
    assert update.annotations.destructive_hint is False and delete.annotations.destructive_hint is True
    assert set(update.input_schema["required"]) == {"workspace", "name"}
    assert set(delete.input_schema["required"]) == {"workspace"}
    assert "63 characters" in update.description and "every member sees the new name" in update.description
    assert "can't be undone" in delete.description and "workspace_not_empty" in delete.description
    assert "finished ones too" in delete.description and "`blockers`" in delete.description
    for tool in (update, delete):
        assert "never picked for you" in tool.description and "a label is refused there" in tool.description
    assert "update_workspace" in tools["workspaces"].description and "delete_workspace" in tools["workspaces"].description


@pytest.mark.parametrize("tool,extra", _BOTH_TOOLS)
@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("confirm", [False, True])
async def test_workspace_writes_never_pick_the_only_workspace(mcp_client, fake, with_key, tool, extra, blank, confirm):
    result = await mcp_client.call_tool(tool, {**extra, "workspace": blank, "confirm": confirm, "operation_id": OP})
    body = _payload(result)
    assert result.is_error and body["code"] == "invalid_argument" and "`workspace` is required" in body["message"]
    assert [c[0] for c in fake["last"].calls] == ["close"]         # the user has one workspace, and it wasn't touched


@pytest.mark.parametrize("tool,extra", _BOTH_TOOLS)
async def test_workspace_writes_take_a_label_on_the_preview_and_tell_the_agent_to_confirm_by_id(mcp_client, fake, with_key,
                                                                                                tool, extra):
    body = _payload(await mcp_client.call_tool(tool, {**extra, "workspace": "ReSearch"}))
    assert body["confirmed"] is False and body["workspace"] == WS
    step = body["next_step"]
    assert f"`workspace` set to the id `{WS}`" in step and "confirm=true" in step and "Never set confirm=true" in step
    assert "same arguments" not in step                            # the same arguments would be the refused label

    done = await mcp_client.call_tool(tool, {**extra, "workspace": WS, "confirm": True,
                                             "operation_id": body["operation_id"]})
    assert not done.is_error and _calls(fake, tool)[0][1] == (WS,)


@pytest.mark.parametrize("tool,extra", _BOTH_TOOLS)
async def test_workspace_writes_refuse_a_label_with_confirm(mcp_client, fake, with_key, tool, extra):
    result = await mcp_client.call_tool(tool, {**extra, "workspace": "research", "confirm": True, "operation_id": OP})
    body = _payload(result)
    assert result.is_error and body["code"] == "invalid_argument" and body["operation_id"] == OP
    assert "'research'" in body["message"] and "the preview's `workspace` field" in body["next_step"]
    assert "keep that id for every retry" in body["next_step"]
    assert [c[0] for c in fake["last"].calls] == ["close"]         # not looked up, and nothing was sent


@pytest.mark.parametrize("tool,extra", _BOTH_TOOLS)
async def test_workspace_writes_look_up_a_label_like_every_other_tool(mcp_client, fake, with_key, tool, extra):
    from conftest import _ws

    result = await mcp_client.call_tool(tool, {**extra, "workspace": "nope"})
    body = _payload(result)
    assert result.is_error and body["code"] == "unknown_workspace" and body["workspaces"][0]["workspace"] == WS

    result = await mcp_client.call_tool(tool, {**extra, "workspace": "all"})
    assert result.is_error and _payload(result)["code"] == "unknown_workspace"     # "all" is no shortcut here

    def twins(inst):
        inst.workspaces = [_ws(WS, "lab"), _ws("fedcba9876543210", "lab")]
    fake["setup"] = twins
    result = await mcp_client.call_tool(tool, {**extra, "workspace": "lab"})
    assert result.is_error and _payload(result)["code"] == "ambiguous_workspace"
    assert not _calls(fake, tool)


@pytest.mark.parametrize("tool,extra", _BOTH_TOOLS)
async def test_workspace_writes_translate_a_failed_label_lookup(mcp_client, fake, with_key, tool, extra):
    fake["setup"] = lambda inst: inst.raise_on.update(list_workspaces=AuthenticationError(401, "API key has expired."))
    result = await mcp_client.call_tool(tool, {**extra, "workspace": "research"})
    assert result.is_error and _payload(result)["code"] == "invalid_api_key"


@pytest.mark.parametrize("tool,extra", _BOTH_TOOLS)
async def test_workspace_writes_confirm_needs_an_operation_id(mcp_client, fake, with_key, tool, extra):
    refused = await mcp_client.call_tool(tool, {**extra, "workspace": WS, "confirm": True})
    assert refused.is_error and _payload(refused)["code"] == "operation_id_required"
    assert not _calls(fake, tool)


@pytest.mark.parametrize("tool,extra,preview_call", [("update_workspace", {"name": "lab-2"}, "estimate_workspace_update"),
                                                     ("delete_workspace", {}, "check_workspace_delete")])
async def test_a_viewer_is_told_on_the_preview_not_after_agreeing(mcp_client, fake, with_key, tool, extra, preview_call):
    fake["setup"] = lambda inst: inst.raise_on.update(
        **{preview_call: PermissionDeniedError(403, "You are not admin of this workspace")})
    result = await mcp_client.call_tool(tool, {**extra, "workspace": WS})
    body = _payload(result)
    assert result.is_error and body["code"] == "forbidden" and "Tell the user" in body["next_step"]


@pytest.mark.parametrize("tool,extra", _BOTH_TOOLS)
async def test_workspace_writes_need_a_write_key_but_their_previews_do_not(mcp_client, fake, with_key, tool, extra):
    fake["setup"] = lambda inst: inst.raise_on.update(
        **{tool: PermissionDeniedError(403, "API key lacks required scope: write")})
    prev = _payload(await mcp_client.call_tool(tool, {**extra, "workspace": WS}))
    assert prev["confirmed"] is False
    result = await mcp_client.call_tool(tool, {**extra, "workspace": WS, "confirm": True,
                                               "operation_id": prev["operation_id"]})
    assert result.is_error and _payload(result)["code"] == "write_scope_required"


@pytest.mark.parametrize("tool,extra", _BOTH_TOOLS)
async def test_workspace_writes_on_a_workspace_that_is_gone(mcp_client, fake, with_key, tool, extra):
    fake["setup"] = lambda inst: inst.raise_on.update(
        **{tool: NotFoundError(404, f"No workspace '{WS}'.", title="Workspace Not Found")})
    result = await mcp_client.call_tool(tool, {**extra, "workspace": WS, "confirm": True, "operation_id": OP})
    assert result.is_error and _payload(result)["code"] == "not_found"


@pytest.mark.parametrize("tool,args", [
    ("create_pod", {"name": "p", "template_id": 1}),
    ("submit_task", {"name": "t", "script": "pass", "image": "img", "cpu_preset": "micro-2c8g"}),
])
@pytest.mark.parametrize("title", ["Workspace not found or not ready", "Workspace is not ready"])
async def test_a_workspace_that_is_not_running_yet_says_to_wait(mcp_client, fake, with_key, tool, args, title):
    fake["setup"] = lambda inst: inst.raise_on.update(
        **{tool: MeshiveAPIError(400, "Workspace 0123456789abcdef is not ready please try later", title=title)})
    result = await mcp_client.call_tool(tool, {**args, "confirm": True, "operation_id": "test-operation-0001"})
    body = _payload(result)
    assert result.is_error and body["code"] == "workspace_not_ready" and body["operation_id"] == "test-operation-0001"
    assert "`workspaces` tool with no arguments" in body["next_step"] and "`running`" in body["next_step"]
    assert "tell the user" in body["next_step"]
