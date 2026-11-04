import json

import httpx
import pytest
from meshive.exceptions import (AuthenticationError, ConfigurationError, MeshiveAPIError, NotFoundError,
                                PermissionDeniedError, RateLimitError)

from meshive_mcp.errors import translate


def _body(exc):
    return json.loads(str(translate(exc)))


def test_auth_errors():
    assert _body(ConfigurationError("x"))["code"] == "no_api_key"
    b = _body(AuthenticationError(401, "API key has expired."))
    assert b["code"] == "invalid_api_key" and "Do not retry" in b["next_step"]


def test_scope_vs_role_forbidden():
    assert _body(PermissionDeniedError(403, "API key lacks required scope: write"))["code"] == "write_scope_required"
    assert _body(PermissionDeniedError(403, "Not an admin"))["code"] == "forbidden"


def test_rate_limit_carries_wait():
    b = _body(RateLimitError(429, "slow down", retry_after=42.0))
    assert b["code"] == "rate_limited" and b["retry_after"] == 42 and "42s" in b["next_step"]


@pytest.mark.parametrize("status,code", [(402, "insufficient_credit"), (409, "conflict"), (400, "invalid_request"),
                                         (503, "temporarily_unavailable"), (500, "server_error"), (418, "api_error")])
def test_status_mapping(status, code):
    assert _body(MeshiveAPIError(status, "m"))["code"] == code


def test_not_found_and_network():
    assert _body(NotFoundError(404, "no pod"))["code"] == "not_found"
    failed = _body(NotFoundError(404, "failed to start: CrashLoopBackOff", title="Pod Creation Failed"))
    assert failed["code"] == "pod_creation_failed" and "CrashLoopBackOff" in failed["message"]
    assert _body(httpx.ConnectError("boom"))["code"] == "temporarily_unavailable"


def test_a_connection_the_api_closed_without_answering_is_temporarily_unavailable():
    b = _body(httpx.RemoteProtocolError("Server disconnected without sending a response."))
    assert b["code"] == "temporarily_unavailable" and b["retry_after"] == 15 and "operation_status" in b["next_step"]
    # A request this side couldn't form is a bug, not the API being away.
    with pytest.raises(httpx.LocalProtocolError):
        translate(httpx.LocalProtocolError("bad header"))


def test_unknown_exception_is_reraised():
    with pytest.raises(KeyError):
        translate(KeyError("bug"))


def _api(status, title, message="m", **detail):
    return MeshiveAPIError(status, message, title=title, raw={"detail": {"title": title, "message": message, **detail}})


@pytest.mark.parametrize("title,code,hint", [
    ("Local Storage Not Attachable", "local_storage_not_attachable", "local_volumes"),
    ("Local Storage Not Supported", "local_storage_not_supported", "local_volumes"),
    ("Local Volume Not Allowed", "local_volume_not_allowed", "Spot pods"),
    ("Command Too Long", "command_too_long", "maxLength"),
    ("CPU Line Not For GPU Pods", "cpu_line_not_for_gpu_pods", "gpu_model"),
    ("vCPU Not Offered", "vcpu_not_offered", "`allowed`"),
    ("RAM Not Offered", "ram_not_offered", "`allowed`"),
    ("Port In Use", "port_in_use", "ports"),
    ("Port Name In Use", "port_name_in_use", "name"),
    ("Port Not In Template", "port_not_in_template", "`templatePorts`"),
    ("Required Env Missing", "required_env_missing", "`keys`"),
    ("Duplicate Mount Path", "duplicate_mount_path", "mount_path"),
    ("Storage Required", "storage_required", "local_volumes"),
])
def test_bad_request_titles_map_to_codes(title, code, hint):
    b = _body(_api(400, title))
    assert b["code"] == code and hint in b["next_step"], b


def test_bad_request_details_reach_the_agent():
    """The allowed sizes and the missing env keys are how the agent fixes the call — they must not be dropped."""
    b = _body(_api(400, "RAM Not Offered", "ram_gb=5 is not offered", allowed=[16, 32, 64]))
    assert b["allowed"] == [16, 32, 64] and b["message"] == "ram_gb=5 is not offered"
    b = _body(_api(400, "Required Env Missing", keys=["HF_TOKEN"]))
    assert b["keys"] == ["HF_TOKEN"]
    b = _body(_api(400, "Command Too Long", maxLength=4096))
    assert b["maxLength"] == 4096
    b = _body(_api(400, "Port Not In Template", "The template does not open port 9999.", templatePorts=[8188, 8888]))
    assert b["code"] == "port_not_in_template" and b["templatePorts"] == [8188, 8888] and "`ports`" in b["next_step"]
    b = _body(_api(400, "Port In Use", port=8188))
    assert b["port"] == 8188 and "template_ports" in b["next_step"]
    b = _body(_api(400, "Some New Rule", allowed=[1, 2]))           # unknown titles keep the generic hint and the detail
    assert b["code"] == "invalid_request" and b["allowed"] == [1, 2]
    assert _body(MeshiveAPIError(422, "m", raw={"detail": [{"loc": ["body"]}]}))["code"] == "invalid_request"


def test_start_conflicts_point_at_wait_placements():
    from meshive.exceptions import ConflictError

    b = _body(ConflictError(409, "No suitable node", title="No Available Node",
                            raw={"detail": {"title": "No Available Node", "message": "No suitable node"}}))
    assert b["code"] == "no_available_node" and "wait_same_node" in b["next_step"] and "wait_any_node" in b["next_step"]
    # The server's own `code` in the detail must not clash with ours (it used to raise TypeError).
    b = _body(ConflictError(409, "saved configuration doesn't match", title="Original node can't run this Pod",
                            raw={"detail": {"title": "Original node can't run this Pod", "message": "x",
                                            "code": "same_node_spec_mismatch"}}))
    assert b["code"] == "same_node_spec_mismatch" and b["server_code"] == "same_node_spec_mismatch"
    assert "any_node" in b["next_step"]


def test_sdk_argument_errors_become_invalid_argument():
    """The SDK checks argument shapes before sending and raises ValueError; the agent gets the reason, not a bare failure."""
    b = _body(ValueError("local_volumes[].mount_path must be an absolute path"))
    assert b["code"] == "invalid_argument" and "absolute path" in b["message"]
    with pytest.raises(UnicodeDecodeError):                          # other ValueError kinds are still treated as bugs
        translate(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"))


def test_data_loss_consent_names_the_placement_to_retry_with():
    """A start without a placement can be refused for data-loss consent; the server's pick is in detail.placement."""
    from meshive.exceptions import ConflictError

    b = _body(ConflictError(409, "Moving this pod deletes files.", title="Data Loss Consent Required",
                            raw={"detail": {"title": "Data Loss Consent Required", "message": "x", "podName": "p-0",
                                            "placement": "wait_any_node", "hasUnpreservedWorkspace": True}}))
    assert b["code"] == "data_loss_consent_required" and b["placement"] == "wait_any_node"
    assert "allow_data_loss=true" in b["next_step"] and "`placement`" in b["next_step"]


@pytest.mark.parametrize("title,code,hint", [
    # update_pod
    ("Nothing To Change", "nothing_to_change", "at least one setting"),
    ("Pod Type Locked", "pod_type_locked", "can't change"),
    ("Template Env Not Removable", "template_env_not_removable", "`env`"),
    ("Template Port Not Removable", "template_port_not_removable", "`template_ports`"),
    ("Env Not Found", "env_not_found", "`remove_env`"),
    ("Port Not Found", "port_not_found", "`remove_ports`"),
    ("Storage Not Attached", "storage_not_attached", "`attached`"),
    ("Storage Already Attached", "storage_already_attached", "`detach_storages`"),
    ("Local Storage On Another Machine", "local_storage_on_another_machine", "current machine"),
    ("Local Volume Not Allowed", "local_volume_not_allowed", "`detach_storages`"),
    ("Template Deleted", "template_deleted", "`template_id`"),
    ("Not With A New Template", "not_with_a_new_template", "new template's"),
    ("Not A Pod Template", "not_a_pod_template", "templates tool"),
    ("Incompatible Hardware", "incompatible_hardware", "type can't change"),
    ("Invalid Pod Settings", "invalid_pod_settings", "preview the edit again"),
    ("Watched folders would be removed", "harvest_roots_orphaned", "drop_watched_folders"),
    # update_storage
    ("Storage Cannot Shrink", "storage_cannot_shrink", "`currentSizeGb`"),
    # model sets
    ("Model Set Unavailable", "model_set_unavailable", "`failed`"),
    ("Model Set Not For Template", "model_set_not_for_template", "`available`"),
    ("Model Sets Not Supported", "model_sets_not_supported", "`selectable`"),
    ("Model Set Added Automatically", "model_set_added_automatically", "Leave `model_set` out"),
    # gated model sets (Hugging Face license)
    ("Hugging Face token required", "hf_token_required", "`hf_token_id`"),
    ("License not accepted", "hf_license_not_accepted", "accept it on the model page"),
    ("Access not approved", "hf_access_not_approved", "requests access"),
    ("Hugging Face token rejected", "hf_token_rejected", "source_credentials"),
    ("Model Set Needs a Token", "model_set_needs_token", "Quick Deploy"),
])
def test_edit_and_model_set_titles_map_to_codes(title, code, hint):
    b = _body(_api(400, title))
    assert b["code"] == code and hint in b["next_step"], b


@pytest.mark.parametrize("title,code,hint", [
    ("Node Migration Required", "node_migration_required", "allow_node_migration=true"),
    ("Cannot Change Resources", "cannot_change_resources", "`detach_storages`"),
    ("Resource values need confirmation", "resource_values_need_confirmation", "`ram_gb`"),
    ("Operation in progress", "operation_in_progress", "Wait until"),
    ("Price Exceeds Cap", "price_exceeds_cap", "cap"),
    ("Price Changed", "price_changed", "without their explicit consent"),
    ("Name Taken", "name_taken", "another name"),
])
def test_edit_conflicts_map_to_codes(title, code, hint):
    from meshive.exceptions import ConflictError

    b = _body(ConflictError(409, "m", title=title, raw={"detail": {"title": title, "message": "m"}}))
    assert b["code"] == code and hint in b["next_step"], b


def test_edit_error_details_reach_the_agent():
    """The paths to drop, the attached storage, the current size and the failed files are how the agent continues."""
    b = _body(_api(400, "Watched folders would be removed", code="harvest_roots_orphaned", paths=["/data/out"]))
    assert b["paths"] == ["/data/out"] and b["server_code"] == "harvest_roots_orphaned"
    b = _body(_api(400, "Storage Not Attached", attached=["pv-a"]))
    assert b["attached"] == ["pv-a"]
    b = _body(_api(400, "Storage Cannot Shrink", currentSizeGb=100))
    assert b["currentSizeGb"] == 100
    failed = [{"slug": "vae", "name": "ae.safetensors", "error": "404"}]
    b = _body(_api(400, "Model Set Unavailable", "Couldn't add FLUX.1 dev", modelSet="flux-dev", failed=failed))
    assert b["failed"] == failed and b["modelSet"] == "flux-dev" and "No pod was created" in b["next_step"]
    b = _body(_api(400, "Model Set Not For Template", available=["flux-dev", "sdxl"]))
    assert b["available"] == ["flux-dev", "sdxl"]


def test_data_loss_consent_without_a_placement_is_about_update_pod():
    """update_pod's refusal names no placement; its advice must not send the agent to start_pod."""
    from meshive.exceptions import ConflictError

    b = _body(ConflictError(409, "Moving deletes files.", title="Data Loss Consent Required",
                            raw={"detail": {"title": "Data Loss Consent Required", "message": "x", "podName": "p-0",
                                            "hasUnpreservedWorkspace": True}}))
    assert b["code"] == "data_loss_consent_required" and "placement" not in b
    assert "update_pod again" in b["next_step"] and "allow_node_migration" in b["next_step"]
    assert "start_pod" not in b["next_step"]


def test_wait_timeout_says_the_server_keeps_going():
    from meshive.exceptions import WaitTimeoutError

    b = _body(WaitTimeoutError("Storage pv-1 was not ready within 60s."))
    assert b["code"] == "wait_timeout" and "still in progress" in b["next_step"] and "don't start it again" in b["next_step"]


def test_storage_connection_error_has_its_own_code():
    import meshive.exceptions

    cls = getattr(meshive.exceptions, "StorageConnectionError", None)
    if cls is None:
        pytest.skip("this SDK reports a failed connection as a phase instead")
    b = _body(cls("pv-1"))
    assert b["code"] == "storage_connection_failed" and "Don't retry" in b["next_step"]


@pytest.mark.parametrize("title", ["Workspace not found or not ready", "Workspace is not ready"])
def test_a_workspace_that_is_not_running_yet_has_its_own_code(title):
    b = _body(_api(400, title, "Workspace 0123456789abcdef is not ready please try later"))
    assert b["code"] == "workspace_not_ready" and "0123456789abcdef" in b["message"]
    assert "`workspaces` tool with no arguments" in b["next_step"] and "`running`" in b["next_step"]
    assert "isn't listed" in b["next_step"] and "tell the user" in b["next_step"]
    assert _body(_api(400, "Workspace does not exist"))["code"] == "invalid_request"      # other titles are untouched


def test_a_workspace_that_is_not_empty_passes_the_blockers_on():
    from meshive.exceptions import ConflictError

    blockers = {"assets": 3, "operations_in_progress": 1}
    b = _body(ConflictError(409, "'lab' still holds: assets (3).", title="Workspace Not Empty",
                            raw={"detail": {"title": "Workspace Not Empty", "message": "'lab' still holds: assets (3).",
                                            "blockers": blockers}}))
    assert b["code"] == "workspace_not_empty" and b["blockers"] == blockers and "'lab'" in b["message"]
    step = b["next_step"]
    assert "Tell the user" in step and "only if the user asks" in step and "managed in the console" in step
    assert "`operations_in_progress` and `task_outputs` clear on their own" in step and "check again shortly" in step
    assert "under `storages` for a few minutes" in step and "include_system=true" in step and "`pending_payouts`" in step
    # What it never does: offer a way past the check, or a force option on a tool. A person has the console, and which
    # button it shows depends on what is left, so none is named.
    assert "Never suggest a way around" in step and "force" not in step.lower() and "Delete everything" not in step
    assert "No tool can delete" in step and "in the console, which removes what's in it for good" in step


def _not_empty_step():
    from meshive.exceptions import ConflictError

    return _body(ConflictError(409, "m", title="Workspace Not Empty"))["next_step"]


def test_a_workspace_that_is_not_empty_sorts_what_tools_can_delete_from_what_the_console_does():
    step = _not_empty_step()
    assert "Pods, storage, servings and custom models can be deleted with their tools" in step
    assert "Assets, finished tasks, credentials, webhooks, custom templates, inference keys" in step
    assert "templates and models" not in step


def test_a_workspace_that_is_not_empty_does_not_say_system_pods_or_kept_volumes_go_on_their_own():
    step = _not_empty_step()
    assert "serving replicas, which go with their serving" in step
    assert "system pods, which go with their storage volume (delete_storage)" in step
    assert "A volume the storages tool still shows stays and bills until it is deleted" in step
    assert "only a deleted pod's hidden temporary volume" in step


def test_a_workspace_that_is_not_empty_says_how_long_each_wait_is_and_when_to_stop():
    step = _not_empty_step()
    assert "usually clears within minutes, at most about 30 minutes" in step
    assert "about 30 minutes after the task ended or when its upload finishes" in step
    assert "stop checking and tell the user" in step and "look at the task in the console or contact support" in step
    # Payouts follow a daily job, so they get their own sentence, outside the "check again shortly" one.
    assert "`pending_payouts` clears once the earnings are paid out, usually within a day" in step
    assert "on hold, or a failed payout, can take longer" in step and "contact support" in step
    assert "Deleting in the console before that loses them" in step


def test_a_refused_workspace_delete_says_why_and_does_not_invite_a_retry():
    b = _body(_api(400, "Unable to delete workspace", "This workspace is protected and cannot be deleted."))
    assert b["code"] == "workspace_not_deletable" and "protected" in b["message"]
    assert "Tell the user" in b["next_step"] and "don't retry" in b["next_step"]


def test_a_workspace_that_is_gone_is_not_found():
    b = _body(NotFoundError(404, "No workspace 'abc'.", title="Workspace Not Found"))
    assert b["code"] == "not_found" and "list tool" in b["next_step"]


def test_a_taken_name_covers_renaming_a_workspace():
    from meshive.exceptions import ConflictError

    b = _body(ConflictError(409, "m", title="Name Taken", raw={"detail": {"title": "Name Taken", "message": "m"}}))
    assert "update_pod, update_storage or update_workspace `name`" in b["next_step"]


# --- hosting -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status,title,code", [
    (400, "Insufficient Notice Period", "insufficient_notice"),
    (400, "Maintenance In Progress", "maintenance_state"),
    (400, "Maintenance Already Done", "maintenance_state"),
    (400, "Maintenance Not Started", "maintenance_state"),
    (400, "Not in Maintenance State", "maintenance_state"),
    (400, "Listing Failed", "listing_refused"),
    (400, "Unlisting Failed", "listing_refused"),
    (400, "Not in Any Setup Stage", "not_retryable"),
    (400, "Too Many Requests", "port_check_cooldown"),
    (400, "Delete In Progress", "delete_in_progress"),
    (400, "Recipient Not Allowed", "recipient_not_allowed"),
    (400, "Message Send Failed", "message_rejected"),
    (409, "Overlapping Maintenance Window", "maintenance_overlap"),
    (409, "Maintenance Not Available", "maintenance_not_available"),
    (409, "Not Retryable", "not_retryable"),
])
def test_host_titles_map_to_codes(status, title, code):
    from meshive.exceptions import ConflictError

    exc = (ConflictError if status == 409 else MeshiveAPIError)(
        status, "the server's reason", title=title, raw={"detail": {"title": title, "message": "the server's reason"}})
    b = _body(exc)
    assert b["code"] == code and b["message"] == "the server's reason" and b["next_step"], b


def test_host_not_found_titles():
    b = _body(NotFoundError(404, "IPMI isn't set up for this machine.", title="Credentials Not Available"))
    assert b["code"] == "credentials_not_available" and "IPMI" in b["message"] and "don't retry" in b["next_step"]
    for title in ("Machine not found", "Maintenance Not Found", "Message Not Found", None):
        assert _body(NotFoundError(404, "gone", title=title))["code"] == "not_found", title


def test_a_rate_limit_named_too_many_requests_stays_rate_limited():
    # Only the port check's cooldown is a 400 with this title; a 429 keeps its own waiting instructions.
    b = _body(RateLimitError(429, "Rate limit exceeded.", title="Too Many Requests", retry_after=12.0))
    assert b["code"] == "rate_limited" and b["retry_after"] == 12


def test_host_refusals_carry_their_own_retry_advice():
    from meshive_mcp.tools._common import _OWN_RETRY_ADVICE_CODES

    assert {"insufficient_notice", "maintenance_overlap", "recipient_not_allowed", "message_rejected", "listing_refused",
            "port_check_cooldown", "delete_in_progress", "maintenance_state"} <= _OWN_RETRY_ADVICE_CODES
