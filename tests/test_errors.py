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
