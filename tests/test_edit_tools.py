"""1.2 tools — editing pods and storage, Connect to storage, model sets: previews, consent gates, SDK arguments, errors."""
import json

import pytest
from meshive.exceptions import ConflictError, MeshiveAPIError, PermissionDeniedError, WaitTimeoutError

pytestmark = pytest.mark.anyio

WS = "0123456789abcdef"
POD = "0123456789abcdef-3"


def _payload(result):
    return json.loads(result.content[0].text)


def _calls(fake, name):
    return [c for c in fake["last"].calls if c[0] == name]


def _api(status, title, message="m", **detail):
    exc = ConflictError if status == 409 else MeshiveAPIError
    return exc(status, message, title=title, raw={"detail": {"title": title, "message": message, **detail}})


# --- update_pod ---------------------------------------------------------------------------------------------------

async def test_update_pod_preview_shows_changes_price_and_restart(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("update_pod", {"pod": POD, "gpu_count": 2, "env": {"HF_TOKEN": "hf_secret_1"},
                                                       "secret_keys": ["HF_TOKEN"]})
    body = _payload(result)
    assert body["confirmed"] is False and body["action"] == "update_pod" and body["pod"] == POD
    assert body["operation_id"] and "confirm=true" in body["next_step"]
    q = body["question"]
    assert "2× RTX 3060" in q and "from $0.500/hour to $1.000/hour" in q
    assert "The pod is running, so it restarts with the new settings." in q
    est = body["estimate"]
    assert est["current_price_per_hour_display"] == "$0.500" and est["price_per_hour_display"] == "$1.000"
    assert est["breakdown_display"] == {"gpu": "$1.000"} and est["restarts"] is True
    assert body["requires_data_loss_consent"] is False and body["data_loss_warning"] == ""
    # Only what changes is sent; the consent flags are not part of the estimate.
    (_, args, kw), = _calls(fake, "estimate_pod_update")
    assert args == (POD, WS) and kw == {"gpu_count": 2, "env": {"HF_TOKEN": "hf_secret_1"}, "secret_keys": ["HF_TOKEN"]}
    assert not _calls(fake, "update_pod") and not _calls(fake, "get_pod")
    assert "hf_secret_1" not in result.content[0].text          # values the agent passed are never echoed back


async def test_update_pod_confirm_sends_the_edit_with_the_operation_id(mcp_client, fake, with_key):
    args = {"pod": POD, "gpu_count": 2, "remove_ports": [6006], "detach_storages": ["pv-a"]}
    prev = _payload(await mcp_client.call_tool("update_pod", args))
    done = _payload(await mcp_client.call_tool("update_pod", {**args, "confirm": True,
                                                              "operation_id": prev["operation_id"]}))
    (_, a, kw), = _calls(fake, "update_pod")
    assert a == (POD, WS) and kw["gpu_count"] == 2 and kw["remove_ports"] == [6006] and kw["detach_storages"] == ["pv-a"]
    assert kw["idempotency_key"] == prev["operation_id"]
    assert kw["allow_node_migration"] is False and kw["allow_data_loss"] is False
    assert "vcpu" not in kw and "name" not in kw                    # None arguments are left to the server
    assert done["transaction_id"] == 15001 and done["renamed_only"] is False and done["operation_id"] == prev["operation_id"]
    assert "restarts with the new settings" in done["next_step"] and "transaction_id=15001" in done["next_step"]
    assert done["estimate"]["price_per_hour_display"] == "$1.000"

    refused = _payload(await mcp_client.call_tool("update_pod", {**args, "confirm": True}))
    assert refused["code"] == "operation_id_required" and not _calls(fake, "update_pod")


async def test_update_pod_rename_only_does_not_restart(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "name": "renamed"}))
    assert "Only the name changes; the pod isn't restarted." in prev["question"]
    assert "price stays at $0.500/hour" in prev["question"] and "restarts with" not in prev["question"]
    done = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "name": "renamed", "confirm": True,
                                                              "operation_id": prev["operation_id"]}))
    assert done["renamed_only"] is True and done["transaction_id"] is None
    assert done["next_step"].startswith("Renamed; the pod wasn't restarted")
    # A price cap guards the edit; it doesn't make a rename a rebuild.
    capped = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "name": "renamed", "max_price_per_hour": 2}))
    assert "Only the name changes" in capped["question"]


async def test_update_pod_on_a_stopped_pod_applies_at_next_start(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: setattr(inst, "pod_running", False)
    prev = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "vcpu": 16}))
    assert "isn't running; it uses the new settings at its next start" in prev["question"]
    done = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "vcpu": 16, "confirm": True,
                                                              "operation_id": prev["operation_id"]}))
    assert "at its next start" in done["next_step"] and "restarts" not in done["next_step"]


async def test_update_pod_preview_prices_new_local_storage(mcp_client, fake, with_key):
    local = [{"name": "scratch", "mount_path": "/scratch", "size_gb": 50}]
    prev = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "local_volumes": local}))
    assert "$0.014/hour for the new local storage" in prev["question"]
    assert prev["estimate"]["breakdown_display"]["local_storage"] == "$0.014"
    assert _calls(fake, "estimate_pod_update")[0][2]["local_volumes"] == local


async def test_update_pod_node_migration_needs_separate_data_loss_consent(mcp_client, fake, with_key):
    """allow_node_migration may move the pod, which deletes unpreserved workspace files — the same gate as start_pod
    any_node: confirm=true alone isn't enough while the pod may have such files."""
    args = {"pod": POD, "gpu_count": 4, "allow_node_migration": True, "confirm": True, "operation_id": "edit-move-0001"}
    blocked = _payload(await mcp_client.call_tool("update_pod", args))
    assert blocked["confirmed"] is False and blocked["requires_data_loss_consent"] is True
    assert "permanently deleted" in blocked["question"] and "allow_data_loss=true" in blocked["data_loss_warning"]
    assert "the pod moves to another machine" in blocked["question"]
    assert _calls(fake, "get_pod") and not _calls(fake, "update_pod")
    # The estimate is asked without the consent flags, so the server prices the edit instead of refusing it.
    assert "allow_node_migration" not in _calls(fake, "estimate_pod_update")[0][2]

    done = _payload(await mcp_client.call_tool("update_pod", {**args, "allow_data_loss": True}))
    assert done["accepted"] is True
    kw = _calls(fake, "update_pod")[0][2]
    assert kw["allow_node_migration"] is True and kw["allow_data_loss"] is True
    assert kw["idempotency_key"] == "edit-move-0001"


async def test_update_pod_migration_without_unpreserved_files_needs_no_extra_consent(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: setattr(inst, "unpreserved_workspace", False)
    args = {"pod": POD, "gpu_count": 4, "allow_node_migration": True, "operation_id": "edit-move-0002"}
    prev = _payload(await mcp_client.call_tool("update_pod", args))
    assert prev["requires_data_loss_consent"] is False and prev["data_loss_warning"] == ""
    _payload(await mcp_client.call_tool("update_pod", {**args, "confirm": True}))
    kw = _calls(fake, "update_pod")[0][2]
    assert kw["allow_node_migration"] is True and kw["allow_data_loss"] is False


async def test_update_pod_sends_allow_data_loss_only_with_node_migration(mcp_client, fake, with_key):
    args = {"pod": POD, "gpu_count": 2, "allow_data_loss": True}
    prev = _payload(await mcp_client.call_tool("update_pod", args))
    assert "allow_data_loss is only sent together with allow_node_migration" in prev["question"]
    assert not _calls(fake, "get_pod")
    _payload(await mcp_client.call_tool("update_pod", {**args, "confirm": True, "operation_id": prev["operation_id"]}))
    assert _calls(fake, "update_pod")[0][2]["allow_data_loss"] is False


async def test_update_pod_node_migration_required_asks_the_user_before_moving(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(update_pod=_api(
        409, "Node Migration Required", "This machine doesn't have room for 4 GPUs."))
    result = await mcp_client.call_tool("update_pod", {"pod": POD, "gpu_count": 4, "confirm": True,
                                                       "operation_id": "edit-grow-0001"})
    body = _payload(result)
    assert result.is_error and body["code"] == "node_migration_required" and body["operation_id"] == "edit-grow-0001"
    step = body["next_step"]
    assert "Nothing was changed" in step and "allow_node_migration=true" in step and "same operation_id" in step
    assert "Any retry MUST" not in step                               # its own advice: call again with the agreed flag


async def test_update_pod_watched_folder_removal_needs_the_users_ok(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(update_pod=_api(
        400, "Watched folders would be removed", "These watched folders would no longer be on any storage",
        code="harvest_roots_orphaned", paths=["/data/out"]))
    body = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "detach_storages": ["pv-a"], "confirm": True,
                                                              "operation_id": "edit-detach-01"}))
    assert body["code"] == "harvest_roots_orphaned" and body["paths"] == ["/data/out"]
    assert body["server_code"] == "harvest_roots_orphaned"
    assert "drop_watched_folders" in body["next_step"] and "Any retry MUST" not in body["next_step"]

    _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "detach_storages": ["pv-a"],
                                                       "drop_watched_folders": ["/data/out"], "operation_id": "x" * 8}))
    assert _calls(fake, "estimate_pod_update")[0][2]["drop_watched_folders"] == ["/data/out"]


async def test_update_pod_server_side_data_loss_refusal_points_at_update_pod(mcp_client, fake, with_key):
    """The server can refuse a move for consent itself (no placement in the detail) — the advice is about update_pod."""
    fake["setup"] = lambda inst: (setattr(inst, "unpreserved_workspace", False), inst.raise_on.update(update_pod=_api(
        409, "Data Loss Consent Required", "Moving deletes files.", podName=POD, hasUnpreservedWorkspace=True)))
    body = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "gpu_count": 4, "allow_node_migration": True,
                                                              "confirm": True, "operation_id": "edit-move-0003"}))
    assert body["code"] == "data_loss_consent_required" and "placement" not in body
    assert "update_pod again" in body["next_step"] and "allow_data_loss=true" in body["next_step"]
    assert "start_pod" not in body["next_step"]


@pytest.mark.parametrize("title,code,hint", [
    ("Pod Type Locked", "pod_type_locked", "can't change"),
    ("Template Env Not Removable", "template_env_not_removable", "`env`"),
    ("Template Port Not Removable", "template_port_not_removable", "`template_ports`"),
    ("Local Storage On Another Machine", "local_storage_on_another_machine", "current machine"),
    ("Nothing To Change", "nothing_to_change", "at least one"),
])
async def test_update_pod_preview_errors_say_what_to_change(mcp_client, fake, with_key, title, code, hint):
    fake["setup"] = lambda inst: inst.raise_on.update(estimate_pod_update=_api(400, title))
    result = await mcp_client.call_tool("update_pod", {"pod": POD, "remove_env": ["A"]})
    body = _payload(result)
    assert result.is_error and body["code"] == code and hint in body["next_step"]
    assert not _calls(fake, "update_pod")


async def test_update_pod_rename_to_a_taken_name(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(estimate_pod_update=_api(409, "Name Taken", "A pod named 'x' exists."))
    body = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "name": "x"}))
    assert body["code"] == "name_taken" and "another name" in body["next_step"]


async def test_update_pod_description_covers_the_console_rules(mcp_client, fake, with_key):
    tool = {t.name: t for t in (await mcp_client.list_tools()).tools}["update_pod"]
    desc = tool.description
    for phrase in ("pass only what changes", "pod type (GPU or CPU), premiums and the image can't change",
                   "Template variables and ports can't be removed", "recommended sizes", "keeps the per-GPU ratio",
                   "console's default", "current machine", "Spot pods can't have local storage",
                   "node_migration_required", "allow_data_loss", "harvest_roots_orphaned", "drop_watched_folders",
                   "Input assets and watched folders are kept", "`ram_gb`"):
        assert phrase in desc, phrase
    props = tool.input_schema["properties"]
    assert {"remove_env", "remove_ports", "detach_storages", "allow_node_migration", "allow_data_loss",
            "drop_watched_folders", "template_id", "max_price_per_hour"} <= set(props)
    assert tool.annotations.destructive_hint is False and tool.annotations.read_only_hint is False


# --- update_storage -----------------------------------------------------------------------------------------------

async def test_update_storage_preview_then_grow(mcp_client, fake, with_key):
    prev = _payload(await mcp_client.call_tool("update_storage", {"storage": "pv-1", "size_gb": 200}))
    assert prev["confirmed"] is False and prev["action"] == "update_storage" and prev["storage"] == "pv-1"
    assert "grow it from 100 GiB to 200 GiB (it can't shrink again)" in prev["question"]
    assert "from $0.010/hour to $0.019/hour" in prev["question"]
    assert prev["estimate"]["current_price_per_hour_display"] == "$0.010"
    assert _calls(fake, "estimate_storage_update")[0][1:] == (("pv-1", WS), {"size_gb": 200})
    assert not _calls(fake, "update_storage")

    done = _payload(await mcp_client.call_tool("update_storage", {"storage": "pv-1", "size_gb": 200, "confirm": True,
                                                                  "operation_id": prev["operation_id"]}))
    (_, args, kw), = _calls(fake, "update_storage")
    assert args == ("pv-1", WS) and kw == {"size_gb": 200, "idempotency_key": prev["operation_id"]}
    assert done["transaction_id"] == 7 and "asynchronously (transaction 7)" in done["next_step"]


async def test_update_storage_rename_and_usage_warning(mcp_client, fake, with_key):
    args = {"storage": "pv-1", "name": "archive", "usage_warning": True, "usage_warning_threshold": 0.8}
    prev = _payload(await mcp_client.call_tool("update_storage", args))
    q = prev["question"]
    assert q.startswith("Update storage 'pv-1': rename it to 'archive', warn when it is 80% full?")
    assert "price stays at $0.010/hour" in q and "grow" not in q
    done = _payload(await mcp_client.call_tool("update_storage", {**args, "confirm": True,
                                                                  "operation_id": prev["operation_id"]}))
    assert done["next_step"] == "Saved; the change applies right away."
    assert _calls(fake, "update_storage")[0][2]["usage_warning_threshold"] == 0.8

    off = _payload(await mcp_client.call_tool("update_storage", {"storage": "pv-1", "usage_warning": False}))
    assert "turn its usage warning off" in off["question"]
    assert _calls(fake, "estimate_storage_update")[0][2] == {"usage_warning": False}


async def test_update_storage_needs_a_change(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("update_storage", {"storage": "pv-1"})
    assert result.is_error and _payload(result)["code"] == "invalid_argument"
    assert "last" not in fake or not _calls(fake, "estimate_storage_update")


async def test_update_storage_cannot_shrink(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(estimate_storage_update=_api(
        400, "Storage Cannot Shrink", "'pv-1' is 100 GiB; storage can grow but not shrink.", currentSizeGb=100))
    body = _payload(await mcp_client.call_tool("update_storage", {"storage": "pv-1", "size_gb": 50}))
    assert body["code"] == "storage_cannot_shrink" and body["currentSizeGb"] == 100 and "grow" in body["next_step"]


# --- connect_storage ----------------------------------------------------------------------------------------------

async def test_connect_storage_preview_changes_nothing_and_has_no_password(mcp_client, fake, with_key):
    result = await mcp_client.call_tool("connect_storage", {"storage": "pv-1"})
    body = _payload(result)
    assert body["confirmed"] is False and body["action"] == "connect_storage"
    assert "Unlock encrypted storage 'data'" in body["question"] and "after 30 minutes" in body["question"]
    assert "one-time SSH password" in body["question"] and body["connection"]["encrypted"] is True
    assert _calls(fake, "get_storage_connection") and not _calls(fake, "connect_storage")
    assert "storage-pw-9" not in result.content[0].text


async def test_connect_storage_returns_the_login_once_without_an_idempotency_key(mcp_client, fake, with_key):
    """Same policy as ssh_access: the password goes to the user in this one response, the browser-terminal URL (which
    carries it) never, and no Idempotency-Key is sent, so the server keeps no replayable copy of the response."""
    prev = _payload(await mcp_client.call_tool("connect_storage", {"storage": "pv-1"}))
    result = await mcp_client.call_tool("connect_storage", {"storage": "pv-1", "confirm": True,
                                                            "operation_id": prev["operation_id"]})
    body = _payload(result)
    assert body["command"] == "ssh -p 2201 gateway@m.example" and body["password"] == "storage-pw-9"
    assert body["expires_at"].startswith("2030-") and "only to the user" in body["note"]
    assert body["phase"] == "ready" and body["encrypted"] is True and body["auto_lock_minutes"] == 30
    assert "locks again by itself after 30 minutes" in body["next_step"]
    assert "ssh" not in body and "web_url" not in result.content[0].text and "c3RvcmFnZS1wdy05" not in result.content[0].text
    (_, args, kw), = _calls(fake, "connect_storage")
    assert args == ("pv-1", WS) and kw == {"wait": True, "timeout": 60.0}
    assert "idempotency_key" not in kw


async def test_connect_storage_wait_is_bounded(mcp_client, fake, with_key):
    _payload(await mcp_client.call_tool("connect_storage", {"storage": "pv-1", "wait": 500, "confirm": True}))
    assert _calls(fake, "connect_storage")[0][2] == {"wait": True, "timeout": 90.0}
    _payload(await mcp_client.call_tool("connect_storage", {"storage": "pv-1", "wait": 0, "confirm": True}))
    assert _calls(fake, "connect_storage")[0][2] == {"wait": False}


async def test_connect_storage_still_preparing_says_to_call_again(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: setattr(inst, "connect_phase", "preparing")
    body = _payload(await mcp_client.call_tool("connect_storage", {"storage": "pv-1", "confirm": True}))
    assert "password" not in body and "still being prepared (preparing)" in body["next_step"]
    assert "confirm=true" in body["next_step"]

    fake["setup"] = lambda inst: setattr(inst, "connect_phase", "error")
    body = _payload(await mcp_client.call_tool("connect_storage", {"storage": "pv-1", "confirm": True}))
    assert "password" not in body and "failed" in body["next_step"] and "Don't retry" in body["next_step"]


async def test_connect_storage_failure_from_the_sdk_gives_no_login(mcp_client, fake, with_key):
    import meshive.exceptions

    cls = getattr(meshive.exceptions, "StorageConnectionError", None)
    if cls is None:
        pytest.skip("this SDK reports a failed connection as a phase instead")
    fake["setup"] = lambda inst: inst.raise_on.update(connect_storage=cls("pv-1"))
    result = await mcp_client.call_tool("connect_storage", {"storage": "pv-1", "confirm": True})
    body = _payload(result)
    assert result.is_error and body["code"] == "storage_connection_failed" and "password" not in body


async def test_connect_storage_wait_timeout_reports_the_current_state(mcp_client, fake, with_key):
    def setup(inst):
        inst.connect_phase = "preparing"
        inst.raise_on["connect_storage"] = WaitTimeoutError("Storage 'pv-1' was not ready within 60s.")
    fake["setup"] = setup
    result = await mcp_client.call_tool("connect_storage", {"storage": "pv-1", "confirm": True})
    body = _payload(result)
    assert not result.is_error and body["phase"] == "preparing" and "still being prepared" in body["next_step"]
    assert _calls(fake, "get_storage_connection")


async def test_connect_storage_plain_storage_and_write_scope(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: setattr(inst, "storage_encrypted", False)
    prev = _payload(await mcp_client.call_tool("connect_storage", {"storage": "pv-1"}))
    assert prev["question"].startswith("Open an SSH login to the files on storage 'data'?")
    done = _payload(await mcp_client.call_tool("connect_storage", {"storage": "pv-1", "confirm": True}))
    assert done["password"] == "storage-pw-9" and done["next_step"] == "Give the command and password to the user."

    fake["setup"] = lambda inst: inst.raise_on.update(connect_storage=PermissionDeniedError(
        403, "API key lacks required scope: write"))
    denied = _payload(await mcp_client.call_tool("connect_storage", {"storage": "pv-1", "confirm": True}))
    assert denied["code"] == "write_scope_required"


async def test_connect_storage_description_states_the_secret_policy(mcp_client, fake, with_key):
    tool = {t.name: t for t in (await mcp_client.list_tools()).tools}["connect_storage"]
    desc = tool.description
    assert "only when the user asks" in desc and "nobody else" in desc and "never run the command yourself" in desc
    assert "read & write key" in desc and "auto_lock_minutes" in desc
    assert tool.annotations.read_only_hint is False


# --- model sets ---------------------------------------------------------------------------------------------------

async def test_model_sets_lists_the_templates_sets(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("model_sets", {"template_id": 457}))
    assert body["workspace"] == WS and body["template_id"] == 457 and body["selectable"] is True
    s, = body["model_sets"]
    assert s["slug"] == "flux-dev" and s["min_vram_gb"] == 16 and s["license_unknown_count"] == 1
    assert s["items"][0]["already_in_workspace"] is True and "raw" not in body
    assert "model_set" in body["next_step"]
    assert ("list_model_sets", (WS, 457), {}) in fake["last"].calls

    fake["setup"] = lambda inst: setattr(inst, "model_sets_selectable", False)
    none = _payload(await mcp_client.call_tool("model_sets", {"template_id": 12}))
    assert none["selectable"] is False and "no model folders" in none["reason"]
    assert "without `model_set`" in none["next_step"]

    desc = {t.name: t.description for t in (await mcp_client.list_tools()).tools}["model_sets"]
    assert "bytes" in desc and "license_unknown_count" in desc and "data" in desc


async def test_estimate_pod_with_a_model_set_shows_it(mcp_client, fake, with_key):
    est = _payload(await mcp_client.call_tool("estimate_pod", {"name": "p", "template_id": 457, "model_set": "flux-dev"}))
    assert _calls(fake, "estimate_pod")[0][2]["model_set"] == "flux-dev"
    assert est["model_set"]["slug"] == "flux-dev" and est["model_set"]["file_count"] == 3
    step = est["next_step"]
    assert "model set 'FLUX.1 dev' (3 files, 32 GiB)" in step
    assert "at least 16 GB of GPU memory (estimated)" in step and "more than the 12 GB of the chosen GPU" in step
    assert "several minutes" in step

    plain = _payload(await mcp_client.call_tool("estimate_pod", {"name": "p", "template_id": 457}))
    assert plain.get("model_set") is None and "model set" not in plain["next_step"]
    assert "model_set" not in _calls(fake, "estimate_pod")[0][2]


async def test_create_pod_with_a_model_set_warns_it_takes_minutes(mcp_client, fake, with_key):
    args = {"name": "p", "template_id": 457, "gpu_model": "RTX 3060", "model_set": "flux-dev"}
    prev = _payload(await mcp_client.call_tool("create_pod", args))
    assert prev["model_set"]["name"] == "FLUX.1 dev"
    q = prev["question"]
    assert "Create pod 'p' at $0.068/hour?" in q and "several minutes" in q
    assert "don't create the pod again" in q and "operation_status" in q
    _payload(await mcp_client.call_tool("create_pod", {**args, "confirm": True, "operation_id": prev["operation_id"]}))
    assert _calls(fake, "create_pod")[0][2]["model_set"] == "flux-dev"

    plain = _payload(await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 457}))
    assert "operation_status" not in plain["question"] and "model_set" not in plain


async def test_model_set_unavailable_means_no_pod_was_created(mcp_client, fake, with_key):
    failed = [{"slug": "flux1-dev-fp8", "name": "flux1-dev-fp8.safetensors", "error": "Upstream returned 404."}]
    fake["setup"] = lambda inst: inst.raise_on.update(create_pod=_api(
        400, "Model Set Unavailable", "Couldn't add FLUX.1 dev", modelSet="flux-dev", failed=failed))
    result = await mcp_client.call_tool("create_pod", {"name": "p", "template_id": 457, "model_set": "flux-dev",
                                                       "confirm": True, "operation_id": "create-set-0001"})
    body = _payload(result)
    assert result.is_error and body["code"] == "model_set_unavailable" and body["failed"] == failed
    assert body["modelSet"] == "flux-dev" and body["operation_id"] == "create-set-0001"
    assert "No pod was created" in body["next_step"] and "reused" in body["next_step"]
    assert "Any retry MUST" not in body["next_step"]


async def test_model_set_not_for_template_lists_the_available_ones(mcp_client, fake, with_key):
    fake["setup"] = lambda inst: inst.raise_on.update(estimate_pod=_api(
        400, "Model Set Not For Template", "'sdxl' is not a model set for template 'ComfyUI'.", available=["flux-dev"]))
    body = _payload(await mcp_client.call_tool("estimate_pod", {"name": "p", "template_id": 457, "model_set": "sdxl"}))
    assert body["code"] == "model_set_not_for_template" and body["available"] == ["flux-dev"]
    assert "model_sets tool" in body["next_step"]


async def test_pod_args_doc_describes_model_set(mcp_client, fake, with_key):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("estimate_pod", "create_pod"):
        assert "model_set" in tools[name].input_schema["properties"], name
        desc = tools[name].description
        assert "`model_set` adds one model set" in desc and "model_set_unavailable" in desc, name


async def test_server_instructions_cover_editing_connect_and_model_sets():
    from meshive_mcp.server import INSTRUCTIONS

    for phrase in ("update_pod, create_storage, update_storage, connect_storage", "node_migration_required",
                   "allow_node_migration=true", "it never shrinks", "pass one `slug` as model_set",
                   "`ssh_access` and `connect_storage` return passwords", "model set names"):
        assert phrase in INSTRUCTIONS, phrase
