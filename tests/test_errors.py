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
    assert _body(httpx.ConnectError("boom"))["code"] == "temporarily_unavailable"


def test_unknown_exception_is_reraised():
    with pytest.raises(KeyError):
        translate(KeyError("bug"))
