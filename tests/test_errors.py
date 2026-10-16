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
