"""Write tools — confirm gate, estimate/summary previews, SDK call arguments, fine-grained 409 mapping, logs."""
import json

import pytest
from meshive.exceptions import ConflictError, InsufficientCreditError, NotFoundError

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
    (_, args, kw), = _calls(fake, "create_pod")
    assert args == ("p", 457) and kw["workspace"] == WS and kw["gpu_count"] == 2 and kw["rental_type"] == "spot"
    assert kw["volumes"] == [{"storage": "pv-a", "mount_path": "/data"}] and kw["max_price_per_hour"] == 0.2
    assert "vcpu" not in kw   # None arguments are left to the SDK defaults


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


async def test_start_pod_preview_hides_secrets_and_says_why_same_node_waits(mcp_client, fake, with_key):
    from conftest import POD_PASSWORD

    fake["setup"] = lambda inst: setattr(inst, "same_node_reason", "capacity")
    result = await mcp_client.call_tool("start_pod", {"pod": "pod-2"})
    assert POD_PASSWORD not in result.content[0].text                 # previews carry the whole Pod too
    prev = _payload(result)
    assert "cannot start on its original machine now (capacity)" in prev["question"]
    assert prev["data_loss_warning"] == ""


async def test_storage_tools(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("create_storage", {"name": "data", "size_gb": 10, "storage_type": "hostPath"}))
    assert prev["confirmed"] is False and prev["estimate"]["max_size_gb"] == 305 and "hostPath" in prev["question"]
    assert "Create 10 GiB hostPath storage" in prev["question"]        # size_gb is GiB (capacity × 1024 MiB)
    done = _payload(await mcp_client.call_tool("create_storage", {"name": "data", "size_gb": 10, "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["transaction_id"] == 5 and "storages tool" in done["next_step"]
    prev = _payload(await mcp_client.call_tool("delete_storage", {"storage": "pv-1"}))
    assert prev["confirmed"] is False and prev["storage"]["linked_pods"] == ["p-1"] and "1 linked pod" in prev["question"]
    # total_size is MiB — appending GB as-is showed "(102400.0 GB, ...", inflated 1024 times.
    assert "(100 GiB, 1 linked pod(s))" in prev["question"] and prev["storage"]["total_size"] == 102400.0
    done = _payload(await mcp_client.call_tool("delete_storage", {"storage": "pv-1", "confirm": True, "operation_id": "test-operation-0001"}))
    assert done["resource"] == "storage" and done["action"] == "delete"


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
    body = _payload(await mcp_client.call_tool("stop_task", {"operation_id": "test-operation-0001", "task": "task_1"}))
    assert body["resource"] == "task"


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
                                                                   "framework": "sglang",
                                                                   "operation_id": "test-operation-0002"}))
    assert done["registration_id"] == 12 and "deploy_serving" in done["next_step"]
    (_, args, kw), = _calls(fake, "register_model")
    assert args == ("Qwen/Qwen3-0.6B",) and kw["framework"] == "sglang" and kw["idempotency_key"] == "test-operation-0002"

    prev = _payload(await mcp_client.call_tool("delete_model", {"registration_id": 12}))
    assert prev["confirmed"] is False and prev["model"]["name"] == "qwen-small" and not _calls(fake, "delete_model")
    missing = await mcp_client.call_tool("delete_model", {"registration_id": 99})
    assert missing.is_error and "not in workspace" in missing.content[0].text
    gone = _payload(await mcp_client.call_tool("delete_model", {"registration_id": 12, "confirm": True,
                                                                 "operation_id": prev["operation_id"]}))
    assert gone["action"] == "delete" and _calls(fake, "delete_model")[0][1][0] == "12"


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
