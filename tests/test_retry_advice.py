"""When an error carries the "reuse this operation_id and the same arguments" retry advice, and the server refusals
that get their own code: the advice belongs only to a failure that may have left a write done or half done."""
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from meshive.exceptions import ConflictError, MeshiveAPIError, NotFoundError

from meshive_mcp.errors import translate

pytestmark = pytest.mark.anyio

OP = "op-retry-advice-0001"
TAIL = "MUST reuse this operation_id"


def _payload(result):
    return json.loads(result.content[0].text)


def _api_error(cls, status, title, message="m"):
    return cls(status, message, title=title, raw={"detail": {"title": title, "message": message}})


def _raise(fake, method, exc):
    fake["setup"] = lambda inst: inst.raise_on.update({method: exc})


async def _create_storage(mcp_client, **over):
    return _payload(await mcp_client.call_tool("create_storage", {"name": "data", "size_gb": 10, "confirm": True,
                                                                  "operation_id": OP, **over}))


async def test_a_server_validation_refusal_has_no_retry_advice(mcp_client, fake, with_key):
    # A 400 is refused before anything is done, and the server releases the operation_id: "fix the arguments" and
    # "reuse the same arguments" would contradict each other.
    _raise(fake, "create_storage", _api_error(MeshiveAPIError, 400, "Invalid Size", "too small"))
    body = await _create_storage(mcp_client)
    assert body["code"] == "invalid_request" and TAIL not in body["next_step"]


# A reused operation_id (422 "Idempotency-Key Reused") has its own advice: test_next_step_regressions.py.
@pytest.mark.parametrize("exc", [
    _api_error(ConflictError, 409, "Operation Outcome Unknown"),
    httpx.ReadTimeout("timed out"),
    MeshiveAPIError(502, "Bad gateway")])
async def test_an_open_outcome_keeps_the_retry_advice(mcp_client, fake, with_key, exc):
    _raise(fake, "create_storage", exc)
    body = await _create_storage(mcp_client)
    assert TAIL in body["next_step"], body["code"]


async def test_an_open_outcome_is_worded_for_any_write(mcp_client, fake, with_key):
    """The server's words for an operation still pending name a task or transaction and a "key", which most writes
    (an account rename, say) don't have and an agent can read as an API key; its internal record id means nothing to
    the agent. The answer has its own words and leaves the record id out."""
    message = ("This operation is pending or needs reconciliation. It will not be executed again. Check the recorded "
               "task/transaction; do not submit a new key.")
    _raise(fake, "create_storage", ConflictError(409, message, title="Operation Outcome Unknown", raw={"detail": {
        "title": "Operation Outcome Unknown", "message": message, "operationRecordId": "a" * 64,
        "transactionId": None, "taskId": None}}))
    body = await _create_storage(mcp_client)
    assert body["code"] == "operation_outcome_unknown"
    assert "operationRecordId" not in body and "a" * 64 not in json.dumps(body)
    for text in (body["message"], body["next_step"]):
        assert "task/transaction" not in text and "new key" not in text
    assert "operation_status" in body["next_step"] and "new operation_id" in body["next_step"]


def test_a_bucket_folder_asset_has_no_download_links():
    """download_links on an asset that links a folder of the user's bucket: no argument gives it links, so the answer
    has its own code and says to read the objects from the bucket instead of retrying."""
    body = json.loads(str(translate(_api_error(MeshiveAPIError, 400, "Reference Asset Not Downloadable"))))
    assert body["code"] == "reference_asset_not_downloadable"
    assert "bucket" in body["next_step"] and "don't retry" in body["next_step"]
    assert "retry once" not in body["next_step"]


async def test_nothing_sent_means_no_retry_advice(mcp_client, fake, with_key):
    # No operation_id on a confirmed write: nothing was sent.
    body = _payload(await mcp_client.call_tool("create_storage", {"name": "data", "size_gb": 10, "confirm": True}))
    assert body["code"] == "operation_id_required" and TAIL not in body["next_step"]
    assert body["operation_id"]                      # it still hands the agent an id to use
    # A preview that fails on a read sends no write either.
    _raise(fake, "get_storage", NotFoundError(404, "no such storage"))
    body = _payload(await mcp_client.call_tool("delete_storage", {"storage": "pv-404"}))
    assert body["code"] == "not_found" and TAIL not in body["next_step"]


async def test_a_capacity_refusal_on_confirm_is_no_capacity(mcp_client, fake, with_key):
    # The machine filled up between the estimate and the confirmation; the arguments were fine.
    _raise(fake, "create_storage", _api_error(MeshiveAPIError, 400, "No Available Cluster"))
    body = await _create_storage(mcp_client)
    assert body["code"] == "no_capacity" and "Fix the arguments" not in body["next_step"]
    assert TAIL not in body["next_step"]


def test_server_titles_with_their_own_code():
    linked = json.loads(str(translate(_api_error(MeshiveAPIError, 400, "Linked Asset Not Downloadable"))))
    assert linked["code"] == "linked_asset_not_downloadable" and "retry" not in linked["next_step"].lower().replace(
        "don't retry", "")
    cluster = json.loads(str(translate(_api_error(MeshiveAPIError, 400, "No available cluster"))))
    assert cluster["code"] == "no_capacity"


def test_monthly_cost_is_rounded_like_other_money():
    from meshive_mcp.tools.assets import _monthly_cost
    cost = _monthly_cost(1412, SimpleNamespace(price_per_gb_month="0.07"))
    assert len(cost["value"].split(".")[-1]) <= 6 and Decimal(cost["value"]) == Decimal("0")
    cost = _monthly_cost(3 * 2 ** 30 + 7, SimpleNamespace(price_per_gb_month="0.07"))
    assert cost["value"] == "0.21" and cost["display"] == "$0.21"
