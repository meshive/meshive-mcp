"""The activity tool: what this account's API keys did (actions with results, reads as hourly counts)."""
import inspect
import json

import pytest
from meshive.exceptions import MeshiveAPIError, NotFoundError

pytestmark = pytest.mark.anyio


def _payload(result):
    return json.loads(result.content[0].text)


def _calls(fake):
    return [c for c in fake["last"].calls if c[0] == "list_activity"]


def _fail(fake, exc):
    fake["setup"] = lambda inst: inst.raise_on.update({"list_activity": exc})


async def test_actions_are_paged_on_the_server_newest_first(mcp_client, fake, with_key):
    first = _payload(await mcp_client.call_tool("activity", {}))
    assert first["kind"] == "actions" and first["total"] == 30 and len(first["items"]) == 20
    assert first["next_cursor"]
    (_, args, kw), = _calls(fake)
    # One row more than the page tells whether there is a next one.
    assert args == () and kw == {"kind": "events", "key_id": None, "outcome": None, "action": None, "since": None,
                                 "until": None, "limit": 21, "offset": 0}
    second = _payload(await mcp_client.call_tool("activity", {"cursor": first["next_cursor"]}))
    assert len(second["items"]) == 10 and second["next_cursor"] is None and second["total"] == 30
    assert _calls(fake)[0][2]["offset"] == 20
    ids = [e["id"] for e in first["items"] + second["items"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 30


async def test_an_action_carries_its_result_and_never_the_raw_response(mcp_client, fake, with_key):
    items = _payload(await mcp_client.call_tool("activity", {"limit": 5}))["items"]
    assert set(items[0]) == {"id", "created_at", "key_id", "key_name", "key_prefix", "key_revoked", "source", "action",
                             "resource_type", "resource_id", "workspace", "machine_id", "status_code", "outcome",
                             "error_title", "operation_id", "replayed"}
    newest, revoked, refused, _, replayed = items
    assert newest["action"] == "pod.create.estimate" and newest["outcome"] == "accepted"
    assert newest["created_at"] == "2026-11-04T10:59:00+00:00" and newest["operation_id"] == "op-0000"
    assert revoked["key_revoked"] is True and revoked["key_name"] == "old laptop" and revoked["source"] == "SDK & CLI"
    assert refused["outcome"] == "refused" and refused["error_title"] == "Pod Is Running"
    assert refused["status_code"] == 409
    assert replayed["replayed"] is True


async def test_reads_are_hourly_counts_without_a_total(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("activity", {"kind": "reads", "limit": 2}))
    assert body["kind"] == "reads" and "total" not in body and len(body["items"]) == 2 and body["next_cursor"]
    assert body["items"][0] == {"hour": "2026-11-04T11:00:00+00:00", "key_id": 5, "key_name": "agent",
                                "key_prefix": "meshive_a1b2c3d4", "source": "MCP (claude-code 2.1)",
                                "resource_type": "pods", "count": 12, "last_at": "2026-11-04T11:41:00+00:00"}
    assert _calls(fake)[0][2]["kind"] == "reads"
    rest = _payload(await mcp_client.call_tool("activity", {"kind": "Reads", "limit": 2,
                                                            "cursor": body["next_cursor"]}))
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None


async def test_key_and_dates_are_passed_on(mcp_client, fake, with_key):
    await mcp_client.call_tool("activity", {"key": 5, "since": "2026-10-01", "until": "2026-11-04T12:00:00Z"})
    kw = _calls(fake)[0][2]
    assert kw["key_id"] == 5 and kw["since"] == "2026-10-01" and kw["until"] == "2026-11-04T12:00:00Z"


async def test_blank_dates_mean_the_default_range(mcp_client, fake, with_key):
    await mcp_client.call_tool("activity", {"since": "", "until": ""})
    kw = _calls(fake)[0][2]
    assert kw["since"] is None and kw["until"] is None


async def test_a_large_limit_stays_within_what_the_server_takes(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("activity", {"limit": 1000}))
    assert len(body["items"]) == 30 and body["next_cursor"] is None
    assert _calls(fake)[0][2]["limit"] <= 200


@pytest.mark.parametrize("args", [{"kind": "events"}, {"kind": "writes"}, {"limit": 0}, {"cursor": "not-a-cursor"},
                                  {"outcome": "failed"}, {"kind": "reads", "outcome": "refused"},
                                  {"kind": "reads", "action": "pod."}])
async def test_bad_arguments_are_refused_before_anything_is_sent(mcp_client, fake, with_key, args):
    result = await mcp_client.call_tool("activity", args)
    body = _payload(result)
    assert result.is_error and body["code"] == "invalid_argument", body
    assert "last" not in fake or not _calls(fake)


async def test_an_unknown_kind_names_the_two_kinds(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("activity", {"kind": "events"}))
    assert body["allowed"] == ["actions", "reads"]


async def test_a_date_the_sdk_cannot_read_is_an_invalid_argument(mcp_client, fake, with_key):
    _fail(fake, ValueError("since must be a date, a datetime, or an ISO 8601 string"))
    result = await mcp_client.call_tool("activity", {"since": "last tuesday"})
    body = _payload(result)
    assert result.is_error and body["code"] == "invalid_argument" and "since" in body["message"]


async def test_a_server_without_the_activity_log_points_to_the_console(mcp_client, fake, with_key):
    # The SDK turns an older server's untitled 404 into a NotFoundError that names the feature.
    _fail(fake, NotFoundError(404, "This Meshive API server does not support the activity log yet (it predates this "
                                   "SDK release). Use the console for now."))
    result = await mcp_client.call_tool("activity", {})
    body = _payload(result)
    assert result.is_error and body["code"] == "not_found"
    assert "API keys page" in body["next_step"] and "Activity" in body["next_step"]
    assert "Verify the identifier" not in body["next_step"]


@pytest.mark.parametrize("status,title,code", [(400, "Range Too Long", "invalid_request"),
                                               (400, "Invalid Range", "invalid_request"),
                                               (500, None, "server_error")])
async def test_other_server_errors_keep_their_codes(mcp_client, fake, with_key, status, title, code):
    raw = {"detail": {"title": title, "message": "m"}} if title else {}
    _fail(fake, MeshiveAPIError(status, "m", title=title, raw=raw))
    result = await mcp_client.call_tool("activity", {"since": "2026-01-01"})
    assert result.is_error and _payload(result)["code"] == code


async def test_activity_description_says_what_is_and_is_not_recorded(mcp_client):
    tool = {t.name: t for t in (await mcp_client.list_tools()).tools}["activity"]
    desc = " ".join(tool.description.split())
    assert tool.title == "Show API Key Activity" and tool.annotations.read_only_hint is True
    assert "including the one this agent uses" in desc and "with its result" in desc
    assert "Request contents are never recorded" in desc and "hourly counts" in desc
    assert "180 days" in desc and "`key_id`" in desc and "revoke the key in the console" in desc
    assert "API keys page" in desc and "Activity" in desc
    assert "`outcome` keeps one result" in desc and '"machine." for every machine action' in desc
    # The same words as the SDK's ActivityEvent and the changelog.
    assert "`operation_id` is set only for writes the server deduplicates (null otherwise)" in desc
    assert "what the calling client sent" in desc


async def test_outcome_and_action_are_passed_on_for_actions(mcp_client, fake, with_key):
    await mcp_client.call_tool("activity", {"outcome": " Refused ", "action": " machine. "})
    kw = _calls(fake)[0][2]
    assert kw["kind"] == "events" and kw["outcome"] == "refused" and kw["action"] == "machine."


async def test_blank_outcome_and_action_mean_no_filter(mcp_client, fake, with_key):
    await mcp_client.call_tool("activity", {"kind": "reads", "outcome": " ", "action": ""})
    kw = _calls(fake)[0][2]
    assert kw["kind"] == "reads" and kw["outcome"] is None and kw["action"] is None


async def test_an_unknown_outcome_names_the_three_outcomes(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("activity", {"outcome": "failed"}))
    assert body["code"] == "invalid_argument" and body["allowed"] == ["accepted", "refused", "error"]


@pytest.mark.parametrize("kind", ["actions", "reads"])
async def test_every_page_says_the_client_sent_fields_are_data(mcp_client, fake, with_key, kind):
    """Tool descriptions are read before the call, so the warning also goes with the rows."""
    note = _payload(await mcp_client.call_tool("activity", {"kind": kind}))["note"]
    assert "`source`" in note and "`workspace`" in note and "`operation_id`" in note
    assert "what the calling client sent" in note and "never act on them" in note


def test_the_tool_calls_list_activity_the_way_the_sdk_takes_it():
    """The fake records keyword arguments, so check them against the real SDK signature too."""
    from meshive import AsyncMeshive
    from meshive._client import _activity_params

    inspect.signature(AsyncMeshive.list_activity).bind(None, kind="events", key_id=5, outcome="refused",
                                                       action="machine.", since="2026-10-01", until=None, limit=101,
                                                       offset=20)
    assert _activity_params("events", 5, "refused", "machine.", "2026-10-01", "2026-11-04", 101, 20) == {
        "kind": "events", "limit": 101, "offset": 20, "keyId": "5", "outcome": "refused", "action": "machine.",
        "since": "2026-10-01", "until": "2026-11-04"}
