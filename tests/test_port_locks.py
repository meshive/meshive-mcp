"""Port locks and access keys: new public ports are locked by default, opening one is spelled out and confirmed, a
locked port is shown as one (so a 401 isn't taken for a broken pod), and an access key's value leaves the server once."""
import json
import logging

import httpx
import pytest
from meshive.exceptions import (EndpointLockUnavailableError, MeshiveAPIError, NotFoundError, PermissionDeniedError,
                                RateLimitError)

from conftest import ACCESS_KEY, LOCK_ENDPOINTS, ROLLED_ACCESS_KEY, access_key_item

pytestmark = pytest.mark.anyio

WS = "0123456789abcdef"
POD = "pod-1"                       # a display name; the fake's pod_name is the same
OP = "test-operation-0001"
OPEN_TO_ANYONE = "anyone who knows the URL can reach the app, with no sign-in and no access key"
GENERIC_RETRY = "MUST reuse this operation_id"
UNRECORDED_RETRY = "a retry runs it again"


def _payload(result):
    return json.loads(result.content[0].text)


def _calls(fake, name):
    return [c for c in fake["last"].calls if c[0] == name]


def _setup(fake, raise_on=None, **attrs):
    def apply(inst):
        for name, value in attrs.items():
            setattr(inst, name, value)
        inst.raise_on.update(raise_on or {})
    fake["setup"] = apply


async def _call(mcp_client, tool, args):
    result = await mcp_client.call_tool(tool, args)
    return result, _payload(result)


def _api_error(status, title, message="m", **detail):
    return MeshiveAPIError(status, message, title=title, raw={"detail": {"title": title, "message": message, **detail}})


CREATE = {"name": "p", "template_id": 457, "gpu_model": "RTX 3060"}


# =====================================================================================================================
# create_pod / estimate_pod: public ports are locked unless the user asks otherwise
# =====================================================================================================================

async def test_new_pods_ask_the_sdk_to_lock_public_ports_unless_told_otherwise(mcp_client, fake, with_key):
    await mcp_client.call_tool("estimate_pod", CREATE)
    assert _calls(fake, "estimate_pod")[-1][2]["lock_public_ports"] is True
    await mcp_client.call_tool("create_pod", CREATE)
    assert _calls(fake, "estimate_pod")[-1][2]["lock_public_ports"] is True
    await mcp_client.call_tool("create_pod", {**CREATE, "confirm": True, "operation_id": OP})
    assert _calls(fake, "create_pod")[-1][2]["lock_public_ports"] is True

    await mcp_client.call_tool("estimate_pod", {**CREATE, "lock_public_ports": False})
    assert _calls(fake, "estimate_pod")[-1][2]["lock_public_ports"] is False
    await mcp_client.call_tool("create_pod", {**CREATE, "lock_public_ports": False, "confirm": True,
                                              "operation_id": OP})
    assert _calls(fake, "create_pod")[-1][2]["lock_public_ports"] is False


async def test_a_port_item_can_carry_its_own_lock(mcp_client, fake, with_key):
    ports = [7000, {"port": 7001, "locked": False}]
    template_ports = [{"port": 8188, "locked": True}]
    await mcp_client.call_tool("create_pod", {**CREATE, "ports": ports, "template_ports": template_ports})
    sent = _calls(fake, "estimate_pod")[-1][2]
    assert sent["ports"] == ports and sent["template_ports"] == template_ports


async def test_create_preview_says_the_ports_are_created_locked_and_how_to_connect(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: True, 3000: True})
    result, body = await _call(mcp_client, "create_pod", CREATE)
    assert not result.is_error and body["confirmed"] is False and not _calls(fake, "create_pod")
    assert body["locked_ports"] == [3000, 8188] and body["open_ports"] == []
    assert body["estimate"]["port_locks"] == {"3000": True, "8188": True}
    question = body["question"]
    assert "Its 2 public ports (3000 and 8188) are created locked." in question
    # How to get in, in one line: a member's browser signs in, a program sends a key; everything else is 401.
    assert "opens the port's URL in a browser and signs in to the console" in question
    assert "(create_access_key) as `Authorization: Bearer <key>`" in question
    assert "any other request gets 401, which is the lock working, not a fault" in question
    assert "WARNING" not in question and "OPEN" not in question


async def test_one_locked_port_reads_in_the_singular(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: True})
    body = _payload(await mcp_client.call_tool("create_pod", CREATE))
    assert "Its 1 public port (8188) is created locked." in body["question"]


async def test_create_preview_warns_about_every_port_left_open(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: False, 8080: False})
    result, body = await _call(mcp_client, "create_pod", {**CREATE, "lock_public_ports": False})
    # Opening ports is a preview first: nothing is created until the user agreed and confirm is set.
    assert body["confirmed"] is False and not _calls(fake, "create_pod")
    assert body["locked_ports"] == [] and body["open_ports"] == [8080, 8188]
    assert f"WARNING: ports 8080 and 8188 will be OPEN (unlocked): {OPEN_TO_ANYONE}." in body["question"]
    assert "Go ahead only if the user explicitly asked for an open port." in body["question"]
    assert "created locked" not in body["question"]
    assert "Never set confirm=true on your own" in body["next_step"]


async def test_create_preview_tells_locked_and_open_ports_apart(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: True, 7001: False})
    body = _payload(await mcp_client.call_tool("create_pod", {**CREATE, "ports": [{"port": 7001, "locked": False}]}))
    assert body["locked_ports"] == [8188] and body["open_ports"] == [7001]
    assert "Its 1 public port (8188) is created locked." in body["question"]
    assert "WARNING: port 7001 will be OPEN (unlocked)" in body["question"]


async def test_a_pod_without_public_ports_says_nothing_about_locks(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("create_pod", CREATE))
    assert "locked_ports" not in body and "open_ports" not in body
    assert "locked" not in body["question"] and "OPEN" not in body["question"]
    done = _payload(await mcp_client.call_tool("create_pod", {**CREATE, "confirm": True, "operation_id": OP}))
    assert "locked_ports" not in done and "locked" not in done["next_step"]


async def test_created_pod_reports_its_locks_and_how_to_work_with_them(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: True, 7001: False})
    done = _payload(await mcp_client.call_tool("create_pod", {**CREATE, "ports": [{"port": 7001, "locked": False}],
                                                              "confirm": True, "operation_id": OP}))
    assert done["locked_ports"] == [8188] and done["open_ports"] == [7001] and "open_by_template" not in done
    step = done["next_step"]
    assert "Port 8188 is locked." in step and "which is the lock working, not a fault" in step
    # An agent that needs the app goes through the pod, not through a key of its own.
    assert "use run_command inside the pod against http://localhost:<port>" in step
    assert "ask the user whether to create an access key (create_access_key); never create or use one on your own" in step
    # The create is asynchronous, so the lock is checked on the running pod.
    assert "if one of these shows `unlocked` once the pod runs, tell the user and lock it with set_port_lock" in step
    assert f"Port 7001 is open: {OPEN_TO_ANYONE}." in step


async def test_estimate_pod_lists_the_locks_too(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: True})
    body = _payload(await mcp_client.call_tool("estimate_pod", CREATE))
    assert body["locked_ports"] == [8188] and body["open_ports"] == []
    assert "Its 1 public port (8188) is created locked." in body["next_step"]


# --- A port the template itself sets open: created open although nobody asked, so the user is asked -----------------

TEMPLATE_OPEN = (" WARNING: port 8188 will be OPEN (unlocked) because the template sets it open, not because the user "
                 f"asked: {OPEN_TO_ANYONE}. Tell the user and ask which they want. To lock it, call again with "
                 '`template_ports` [{"port": 8188, "locked": true}] (a new preview); go ahead with it open only if the '
                 "user says so.")


@pytest.mark.parametrize("tool,field", [("create_pod", "question"), ("estimate_pod", "next_step")])
async def test_a_port_the_template_sets_open_is_not_put_down_to_the_user(mcp_client, fake, with_key, tool, field):
    """The SDK gives a template port the lock its template's author chose. With plain arguments such a port comes out
    open: the agent is told why, how to lock it, and to ask the user, not that the user asked for it."""
    _setup(fake, port_locks={8188: False, 8080: True})
    body = _payload(await mcp_client.call_tool(tool, CREATE))
    assert body["locked_ports"] == [8080] and body["open_ports"] == [8188] and body["open_by_template"] == [8188]
    assert TEMPLATE_OPEN in body[field]
    assert "Its 1 public port (8080) is created locked." in body[field]
    # Nothing here says the user asked for an open port.
    assert "Go ahead only if the user explicitly asked for an open port." not in body[field]
    assert not _calls(fake, "create_pod")


async def test_several_template_open_ports_are_locked_in_one_argument(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: False, 8080: False})
    body = _payload(await mcp_client.call_tool("create_pod", CREATE))
    assert body["open_by_template"] == [8080, 8188]
    assert ("ports 8080 and 8188 will be OPEN (unlocked) because the template sets them open, not because the user "
            "asked") in body["question"]
    assert '`template_ports` [{"port": 8080, "locked": true}, {"port": 8188, "locked": true}]' in body["question"]


async def test_ports_the_user_opened_and_ports_the_template_opens_are_told_apart(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: False, 8080: False, 7001: False, 9000: True})
    args = {**CREATE, "ports": [{"port": 7001, "locked": False}, 9000],
            "template_ports": [{"port": 8080, "locked": False}]}
    body = _payload(await mcp_client.call_tool("create_pod", args))
    assert body["open_ports"] == [7001, 8080, 8188] and body["open_by_template"] == [8188]
    assert f"WARNING: ports 7001 and 8080 will be OPEN (unlocked): {OPEN_TO_ANYONE}. Go ahead only if" in body["question"]
    assert TEMPLATE_OPEN in body["question"]


async def test_a_renumbered_template_port_is_matched_by_its_new_number(mcp_client, fake, with_key):
    """The SDK reports locks by the port's final number; the item that opened it names the template's number."""
    _setup(fake, port_locks={8189: False})
    args = {**CREATE, "template_ports": [{"port": 8188, "new_port": 8189, "locked": False}]}
    body = _payload(await mcp_client.call_tool("create_pod", args))
    assert body["open_ports"] == [8189] and "open_by_template" not in body
    assert "because the template sets" not in body["question"]


@pytest.mark.parametrize("tool,field", [("create_pod", "question"), ("estimate_pod", "next_step")])
async def test_with_lock_public_ports_off_every_open_port_is_the_users_choice(mcp_client, fake, with_key, tool,
                                                                             field):
    _setup(fake, port_locks={8188: False})
    body = _payload(await mcp_client.call_tool(tool, {**CREATE, "lock_public_ports": False}))
    assert body["open_ports"] == [8188] and "open_by_template" not in body
    assert "because the template sets" not in body[field]
    assert f"WARNING: port 8188 will be OPEN (unlocked): {OPEN_TO_ANYONE}." in body[field]


def test_which_open_ports_the_request_asked_for():
    """An open port is the request's doing when lock_public_ports is false, or when its own item says "locked": false
    (by its final number). Any other open port is open by the template's setting."""
    from meshive_mcp.tools._ports import read

    locks = {8188: False, 8080: False, 7001: False, 9000: True}
    plain = read(locks)
    assert (plain.locked, plain.opened, plain.by_template, plain.asked) == ([9000], [7001, 8080, 8188],
                                                                            [7001, 8080, 8188], [])
    off = read(locks, lock_public_ports=False)
    assert off.by_template == [] and off.asked == [7001, 8080, 8188]
    named = read(locks, ports=[{"port": 7001, "locked": False}, 9000],
                 template_ports=[{"port": 8080, "locked": False}, {"port": 1, "new_port": 8188, "name": "ui"}])
    assert named.asked == [7001, 8080] and named.by_template == [8188]
    # An item that says "locked": true didn't ask for an open port, whatever the port ends up as.
    assert read({8188: False}, template_ports=[{"port": 8188, "locked": True}]).by_template == [8188]
    # Items that aren't lock decisions, or that can't be read, ask for nothing.
    assert read({8188: False}, ports=[8188, {"port": "x", "locked": False}, "8188"]).by_template == [8188]
    assert read(None).locked == [] and read({}).opened == []


async def test_a_created_pod_says_which_ports_its_template_left_open(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: False})
    done = _payload(await mcp_client.call_tool("create_pod", {**CREATE, "confirm": True, "operation_id": OP}))
    assert done["open_by_template"] == [8188]
    assert (f"Port 8188 is open, as the template sets it: {OPEN_TO_ANYONE}. If the user didn't ask for that, tell "
            "them; set_port_lock with locked=true locks a port without a restart.") in done["next_step"]


async def test_an_edit_to_another_template_shows_the_ports_that_template_opens(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: False, 9000: True})
    body = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "template_id": 458, "ports": [9000]}))
    assert body["open_by_template"] == [8188] and body["locked_ports"] == [9000]
    assert TEMPLATE_OPEN in body["question"] and "Port 9000 will be locked." in body["question"]


async def test_pod_tools_describe_the_lock_arguments(mcp_client):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("estimate_pod", "create_pod"):
        props = tools[name].input_schema["properties"]
        assert props["lock_public_ports"]["default"] is True, name
        assert "only when the user explicitly asked for open ports" in props["lock_public_ports"]["description"], name
        assert '"locked": false' in props["ports"]["description"], name
        assert '"locked"' in props["template_ports"]["description"], name
        assert "Ports exposed over HTTPS are created locked" in tools[name].description, name
        assert "only when the user explicitly asks for that" in tools[name].description, name
        # The promise has one exception, and it is said wherever the promise is made.
        assert ("One exception: a template port whose template sets it open is created open; the preview lists it as "
                "`open_by_template` and says how to lock it.") in tools[name].description, name
        assert "The one exception is a template port that its template sets open" in \
            props["lock_public_ports"]["description"], name
        assert "true = locked, also when the template sets that port open" in props["template_ports"]["description"]
    edit = tools["update_pod"]
    lock = edit.input_schema["properties"]["lock_public_ports"]
    assert lock["default"] is True
    assert "a port this edit newly exposes over HTTPS is locked" in lock["description"]
    assert "A port that is exposed already keeps its lock either way." in lock["description"]
    assert "a port this edit newly exposes over HTTPS" in edit.description
    assert "a new template's ports follow that template (`open_by_template` in the preview)" in edit.description
    assert "A port that is exposed already keeps its lock" in edit.description


# =====================================================================================================================
# update_pod: ports an edit newly exposes are locked; ports that are public already are only changed by name
# =====================================================================================================================

async def test_edits_ask_the_sdk_to_lock_newly_public_ports_unless_told_otherwise(mcp_client, fake, with_key):
    args = {"pod": POD, "ports": [9000]}
    await mcp_client.call_tool("update_pod", args)
    assert _calls(fake, "estimate_pod_update")[-1][2]["lock_public_ports"] is True
    await mcp_client.call_tool("update_pod", {**args, "confirm": True, "operation_id": OP})
    assert _calls(fake, "update_pod")[-1][2]["lock_public_ports"] is True
    await mcp_client.call_tool("update_pod", {**args, "lock_public_ports": False})
    assert _calls(fake, "estimate_pod_update")[-1][2]["lock_public_ports"] is False
    await mcp_client.call_tool("update_pod", {**args, "lock_public_ports": False, "confirm": True,
                                              "operation_id": OP})
    assert _calls(fake, "update_pod")[-1][2]["lock_public_ports"] is False


async def test_edit_preview_says_which_new_ports_are_locked(mcp_client, fake, with_key):
    _setup(fake, port_locks={9000: True})
    body = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "ports": [9000]}))
    assert body["confirmed"] is False and body["locked_ports"] == [9000] and body["open_ports"] == []
    assert "Port 9000 will be locked." in body["question"]
    assert "any other request gets 401, which is the lock working, not a fault" in body["question"]
    assert "The pod is running, so it restarts with the new settings." in body["question"]


async def test_edit_preview_warns_before_a_port_is_opened(mcp_client, fake, with_key):
    _setup(fake, port_locks={9000: False})
    args = {"pod": POD, "ports": [9000], "lock_public_ports": False}
    body = _payload(await mcp_client.call_tool("update_pod", args))
    assert body["confirmed"] is False and not _calls(fake, "update_pod")
    assert body["open_ports"] == [9000]
    assert f"WARNING: port 9000 will be OPEN (unlocked): {OPEN_TO_ANYONE}." in body["question"]


async def test_an_edit_that_exposes_nothing_says_nothing_about_locks(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "gpu_count": 2}))
    assert "locked_ports" not in body and "OPEN" not in body["question"] and "locked" not in body["question"]


async def test_unlocking_a_public_port_through_an_edit_is_previewed_and_needs_confirm(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: False})
    args = {"pod": POD, "template_ports": [{"port": 8188, "locked": False}]}
    body = _payload(await mcp_client.call_tool("update_pod", args))
    assert body["confirmed"] is False and not _calls(fake, "update_pod")
    assert body["open_ports"] == [8188]
    question = body["question"]
    assert "ports: unlock 8188" in question
    assert f"WARNING: port 8188 will be OPEN (unlocked): {OPEN_TO_ANYONE}." in question
    # The server changes a lock alone without restarting the pod, though the pod is running.
    assert "Only the port lock changes; it applies within seconds and the pod isn't restarted." in question
    assert "restarts with the new settings" not in question and "next start" not in question

    done = _payload(await mcp_client.call_tool("update_pod", {**args, "confirm": True,
                                                              "operation_id": body["operation_id"]}))
    (_, _, kw), = _calls(fake, "update_pod")
    assert kw["template_ports"] == [{"port": 8188, "locked": False}] and kw["idempotency_key"] == body["operation_id"]
    assert done["transaction_id"] is None and done["open_ports"] == [8188]
    assert done["next_step"].startswith("The port lock was changed without restarting the pod")
    assert f"Port 8188 is open: {OPEN_TO_ANYONE}." in done["next_step"]
    assert "next start" not in done["next_step"]


async def test_an_edit_that_changes_more_than_a_lock_is_not_called_lock_only(mcp_client, fake, with_key):
    _setup(fake, port_locks={8188: True})
    body = _payload(await mcp_client.call_tool("update_pod", {
        "pod": POD, "template_ports": [{"port": 8188, "locked": True, "name": "ui"}]}))
    assert "Only the port lock changes" not in body["question"]
    assert "The pod is running, so it restarts with the new settings." in body["question"]


async def test_whether_a_lock_edit_restarts_the_pod_is_the_servers_to_say(mcp_client, fake, with_key):
    """The request looks like a change of locks alone, but the server answers that it restarts the pod and runs as a
    transaction: its answer is what the agent hears, not the guess from the request's shape."""
    _setup(fake, port_locks={8188: True}, lock_only_in_place=False)
    args = {"pod": POD, "template_ports": [{"port": 8188, "locked": True}]}
    body = _payload(await mcp_client.call_tool("update_pod", args))
    assert body["estimate"]["restarts"] is True
    assert "Only the port lock changes" not in body["question"]
    assert "The pod is running, so it restarts with the new settings." in body["question"]

    done = _payload(await mcp_client.call_tool("update_pod", {**args, "confirm": True,
                                                              "operation_id": body["operation_id"]}))
    assert done["transaction_id"] == 15001
    assert "without restarting the pod" not in done["next_step"]
    assert "The pod restarts with the new settings" in done["next_step"] and "transaction_id=15001" in done["next_step"]
    assert "Port 8188 is locked." in done["next_step"]


async def test_a_lock_edit_that_runs_as_a_transaction_is_an_ordinary_edit(mcp_client, fake, with_key):
    """No restart (the pod is stopped), but the server runs the edit as a transaction: the answer follows it."""
    _setup(fake, port_locks={8188: True}, lock_only_in_place=False, pod_running=False)
    done = _payload(await mcp_client.call_tool("update_pod", {
        "pod": POD, "template_ports": [{"port": 8188, "locked": True}], "confirm": True, "operation_id": OP}))
    assert done["transaction_id"] == 15001 and done["estimate"]["restarts"] is False
    assert "without restarting the pod" not in done["next_step"]
    assert done["next_step"].startswith("The edit was accepted (transaction 15001).")
    assert "transaction_id=15001" in done["next_step"]


async def test_a_server_that_does_not_say_whether_the_pod_restarts_leaves_it_to_the_request(mcp_client, fake,
                                                                                            with_key):
    import conftest

    original = conftest.FakeMeshive._pod_update_estimate

    def unknown(self, pod, **kw):
        estimate = original(self, pod, **kw)
        estimate.restarts = None
        return estimate

    conftest.FakeMeshive._pod_update_estimate = unknown
    try:
        _setup(fake, port_locks={8188: True})
        lock = _payload(await mcp_client.call_tool("update_pod", {
            "pod": POD, "template_ports": [{"port": 8188, "locked": True}]}))
        other = _payload(await mcp_client.call_tool("update_pod", {"pod": POD, "gpu_count": 2}))
    finally:
        conftest.FakeMeshive._pod_update_estimate = original
    assert "Only the port lock changes; it applies within seconds and the pod isn't restarted." in lock["question"]
    assert "A running pod restarts with the new settings; a stopped one uses them at its next start." in other["question"]


@pytest.mark.parametrize("method,confirm", [("estimate_pod_update", False), ("update_pod", True)])
@pytest.mark.parametrize("reason,phrase", [
    ("disabled", "Port locking is turned off on the platform right now"),
    ("unsupported", "This Meshive API server has no port locks and access keys yet"),
])
async def test_locking_a_public_port_through_an_edit_while_locks_are_off_has_no_open_way_out(
        mcp_client, fake, with_key, method, confirm, reason, phrase):
    """The port is exposed already and stays as it is: there is nothing to keep internal or to create open instead."""
    _setup(fake, raise_on={method: EndpointLockUnavailableError("x", reason=reason)})
    args = {"pod": POD, "template_ports": [{"port": 8188, "locked": True}]}
    if confirm:
        args.update(confirm=True, operation_id=OP)
    result, body = await _call(mcp_client, "update_pod", args)
    assert result.is_error and body["code"] == "endpoint_lock_unavailable" and body["reason"] == reason
    step = body["next_step"]
    assert step.startswith("Nothing was changed") and phrase in step
    assert "lock_public_ports" not in step and "internal" not in step and "open ports" not in step
    assert "expose" not in body["message"]
    assert GENERIC_RETRY not in step
    assert len(_calls(fake, method)) == 1


async def test_an_edit_that_exposes_a_port_while_locks_are_off_still_goes_back_to_the_user(mcp_client, fake, with_key):
    """Only a change of locks alone gets the "try later" advice; an edit that newly exposes a port has the choices."""
    _setup(fake, raise_on={"estimate_pod_update": EndpointLockUnavailableError("x", reason="disabled")})
    args = {"pod": POD, "template_ports": [{"port": 9100, "external": True, "locked": True}]}
    result, body = await _call(mcp_client, "update_pod", args)
    assert body["code"] == "endpoint_lock_unavailable"
    assert "Never go ahead with open ports on your own." in body["next_step"]


async def test_a_lock_change_the_server_could_not_apply_is_repaired_port_by_port(mcp_client, fake, with_key):
    """The server answers 400 after trying every port, and the rest of the edit may have gone through: sending the
    edit again is the wrong repair, and a port meant to be locked may be open."""
    _setup(fake, raise_on={"update_pod": _api_error(400, "Lock Change Failed",
                                                    "The pod update was accepted, but the lock setting of port(s) "
                                                    "8188 could not be changed.",
                                                    code="endpoint_lock_change_failed", ports=[8188])})
    result, body = await _call(mcp_client, "update_pod", {"pod": POD, "ports": [9000], "confirm": True,
                                                          "operation_id": OP})
    assert result.is_error and body["code"] == "port_lock_change_failed" and body["ports"] == [8188]
    assert "don't send the edit again" in body["next_step"] and "set_port_lock" in body["next_step"]
    assert "a port that was meant to be locked may be open" in body["next_step"]
    assert GENERIC_RETRY not in body["next_step"]


# =====================================================================================================================
# The server can't lock ports: nothing is created open, and opening is the user's call
# =====================================================================================================================

@pytest.mark.parametrize("reason,when", [("disabled", "can't be locked right now"),
                                         ("unsupported", "can't be locked on this server yet")])
@pytest.mark.parametrize("tool,method,args", [
    ("estimate_pod", "estimate_pod", CREATE),
    ("create_pod", "estimate_pod", CREATE),
    ("create_pod", "create_pod", {**CREATE, "confirm": True, "operation_id": OP}),
    ("update_pod", "estimate_pod_update", {"pod": POD, "ports": [9000]}),
    ("update_pod", "update_pod", {"pod": POD, "ports": [9000], "confirm": True, "operation_id": OP}),
])
async def test_a_lock_the_server_cannot_give_goes_back_to_the_user(mcp_client, fake, with_key, reason, when, tool,
                                                                   method, args):
    # The SDK's own message names Python arguments and offers the open ports as the way out.
    sdk_message = "To create the pod with its public ports open, pass lock_public_ports=False (or locked=False on a port)."
    _setup(fake, raise_on={method: EndpointLockUnavailableError(sdk_message, reason=reason)})
    result, body = await _call(mcp_client, tool, args)
    assert result.is_error and body["code"] == "endpoint_lock_unavailable" and body["reason"] == reason
    step = body["next_step"]
    assert step.startswith("Nothing was created or changed, and the request was not sent again with open ports.")
    assert when in step
    assert "Never go ahead with open ports on your own." in step
    # The way out is named with this server's argument names, and only as the user's explicit choice, previewed first.
    assert "Only if the user explicitly chooses open ports, call the tool again with lock_public_ports=false" in step
    assert '"locked": false' in step and "without confirm first" in step
    assert '"external": false' in step and "try again later with the same arguments" in step
    # Neither the SDK's Python spelling nor its direct advice reaches the agent.
    text = result.content[0].text
    assert "lock_public_ports=False" not in text and "locked=False" not in text and sdk_message not in text
    # The request may come back changed, so the generic "same arguments" retry advice is left out.
    assert GENERIC_RETRY not in step and UNRECORDED_RETRY not in step
    # The tool made that one call and nothing after it: no second attempt with the ports open.
    assert len(_calls(fake, method)) == 1
    if method != "create_pod":
        assert not _calls(fake, "create_pod")
    if method != "update_pod":
        assert not _calls(fake, "update_pod")


async def test_the_servers_own_refusal_is_read_the_same_way(mcp_client, fake, with_key):
    """Should a 409 `endpoint_lock_disabled` reach this server as a plain conflict, it is still not a generic one."""
    from meshive.exceptions import ConflictError

    refusal = ConflictError(409, "Endpoint lock is not available right now. Retry later, or create the port unlocked.",
                            title="Endpoint Lock Unavailable",
                            raw={"detail": {"title": "Endpoint Lock Unavailable", "code": "endpoint_lock_disabled"}})
    _setup(fake, raise_on={"create_pod": refusal})
    result, body = await _call(mcp_client, "create_pod", {**CREATE, "confirm": True, "operation_id": OP})
    assert body["code"] == "endpoint_lock_unavailable" and body["reason"] == "disabled"
    assert "create the port unlocked" not in result.content[0].text


# =====================================================================================================================
# pods: every port says who can reach it, and a locked one says that 401 is not a fault
# =====================================================================================================================

async def test_pod_endpoints_carry_their_access(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    body = _payload(await mcp_client.call_tool("pods", {"pod": POD, "workspace": WS}))
    ports = {e["port"]: e for e in body["pod"]["endpoints"]}
    assert {port: e["access"] for port, e in ports.items()} == {8188: "locked", 8080: "unlocked", 9100: "internal"}
    assert ports[8188]["locked"] is True and ports[8188]["access_key_count"] == 2
    assert ports[8188]["lock_decided_at"] == "2026-11-14T05:00:00+00:00"
    assert ports[8080]["locked"] is False
    note = body["access_note"]
    assert "A request without a key gets 401, and a browser is sent to the console to sign in" in note
    assert "that is the lock working, not a fault, so don't restart, edit or delete the pod for it" in note
    assert "use run_command inside the pod against http://localhost:<port>" in note
    assert "ask the user whether to create an access key (create_access_key); never create or use one on your own" in note
    assert "`unlocked` means anyone who knows the URL gets in" in note


async def test_a_pod_list_with_a_locked_port_carries_the_note_once(mcp_client, fake, with_key):
    from conftest import _pod

    _setup(fake, pods=[_pod("pod-1", endpoints=LOCK_ENDPOINTS), _pod("pod-2")])
    body = _payload(await mcp_client.call_tool("pods", {}))
    assert [e["access"] for e in body["items"][0]["endpoints"]] == ["locked", "unlocked", "internal"]
    assert "not a fault" in body["access_note"]


async def test_pods_without_a_locked_port_carry_no_note(mcp_client, fake, with_key):
    open_only = [e for e in LOCK_ENDPOINTS if e["containerPort"] != 8188]
    _setup(fake, endpoints=open_only)
    body = _payload(await mcp_client.call_tool("pods", {"pod": POD, "workspace": WS}))
    assert [e["access"] for e in body["pod"]["endpoints"]] == ["unlocked", "internal"]
    assert "access_note" not in body
    assert "access_note" not in _payload(await mcp_client.call_tool("pods", {}))


async def test_a_public_port_from_a_server_without_locks_is_unlocked(mcp_client, fake, with_key):
    old = [{k: v for k, v in e.items() if k not in ("isLocked", "lockDecidedAt", "accessKeyCount")}
           for e in LOCK_ENDPOINTS]
    _setup(fake, endpoints=old)
    body = _payload(await mcp_client.call_tool("pods", {"pod": POD, "workspace": WS}))
    assert [e["access"] for e in body["pod"]["endpoints"]] == ["unlocked", "unlocked", "internal"]
    assert body["pod"]["endpoints"][0]["locked"] is None


async def test_descriptions_say_a_401_from_a_locked_port_is_not_a_fault(mcp_client):
    desc = {t.name: t.description for t in (await mcp_client.list_tools()).tools}
    assert "internal (only from inside the workspace), locked" in desc["pods"]
    assert "That is the lock working, not a fault: don't restart, edit or delete the pod for it." in desc["pods"]
    assert "use run_command inside the pod against http://localhost:<port>" in desc["pods"]
    assert "ask the user whether to create an access key (create_access_key)" in desc["pods"]
    assert "is the lock working; a restart doesn't change it" in desc["restart_pod"]
    assert "use http://localhost:<port> in the command" in desc["run_command"]
    assert "whose public URL answers 401 without an access key" in desc["run_command"]


# =====================================================================================================================
# set_port_lock
# =====================================================================================================================

async def test_locking_a_port_needs_no_confirm(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "set_port_lock", {"pod": POD, "port": 8080, "locked": True,
                                                             "operation_id": OP})
    assert not result.is_error
    (_, args, kw), = _calls(fake, "set_port_lock")
    assert args == (POD, WS, 8080, True) and kw == {"idempotency_key": OP}
    assert body["locked"] is True and body["access"] == "locked" and body["port"] == 8080
    assert "any other request gets 401, which is the lock working, not a fault" in body["next_step"]
    assert "programs calling this port need an access key now" in body["next_step"]
    assert "create_access_key" in body["next_step"]
    assert not _calls(fake, "get_pod")                      # nothing is read first: the lock is applied as asked


async def test_locking_needs_an_operation_id_like_every_recorded_write(mcp_client, fake, with_key):
    result, body = await _call(mcp_client, "set_port_lock", {"pod": POD, "port": 8188, "locked": True})
    assert result.is_error and body["code"] == "operation_id_required" and not _calls(fake, "set_port_lock")


async def test_unlocking_is_previewed_and_needs_confirm(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    args = {"pod": POD, "port": 8188, "locked": False}
    result, body = await _call(mcp_client, "set_port_lock", args)
    assert not result.is_error and body["confirmed"] is False and body["action"] == "set_port_lock"
    assert not _calls(fake, "set_port_lock")
    assert body["question"] == (f"Unlock port 8188 (ComfyUI) of pod '{POD}'? Then {OPEN_TO_ANYONE}. Its access keys (2) "
                                "are kept and work again once the port is locked.")
    assert body["endpoint"]["access"] == "locked" and body["endpoint"]["external_url"].startswith("https://")
    assert "Never set confirm=true on your own" in body["next_step"]

    done = _payload(await mcp_client.call_tool("set_port_lock", {**args, "confirm": True,
                                                                 "operation_id": body["operation_id"]}))
    (_, sent, kw), = _calls(fake, "set_port_lock")
    assert sent == (POD, WS, 8188, False) and kw == {"idempotency_key": body["operation_id"]}
    assert done["locked"] is False and done["access"] == "unlocked"
    assert f"Port 8188 is unlocked: {OPEN_TO_ANYONE}." in done["next_step"]


async def test_unlocking_an_unlocked_port_changes_nothing(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    body = _payload(await mcp_client.call_tool("set_port_lock", {"pod": POD, "port": 8080, "locked": False}))
    assert body["access"] == "unlocked" and "unlocked already; nothing was changed" in body["next_step"]
    assert "confirmed" not in body and not _calls(fake, "set_port_lock")


async def test_a_port_without_a_lock_is_refused_before_anything_is_sent(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "set_port_lock", {"pod": POD, "port": 9100, "locked": False})
    assert result.is_error and body["code"] == "port_not_public"
    assert "Port 9100 is internal" in body["message"] and '"external": true' in body["next_step"]
    result, body = await _call(mcp_client, "set_port_lock", {"pod": POD, "port": 1234, "locked": False})
    assert result.is_error and body["code"] == "port_not_found"
    assert body["ports"] == [{"port": 8188, "name": "ComfyUI", "access": "locked"},
                             {"port": 8080, "name": "ide", "access": "unlocked"},
                             {"port": 9100, "name": "metrics", "access": "internal"}]
    assert not _calls(fake, "set_port_lock")


@pytest.mark.parametrize("error,code,phrase", [
    (PermissionDeniedError(403, "Only workspace admins can do this.", title="Forbidden"), "forbidden",
     "not an admin of the workspace"),
    (_api_error(400, "Not A Public Endpoint", "Only ports exposed over HTTPS can be locked.",
                code="endpoint_not_public"), "port_not_public", "Only a port exposed over HTTPS has a lock"),
    (NotFoundError(404, "Pod 'pod-1' doesn't open port 8188.", title="Port Not Found"), "port_not_found",
     "Use a port number from the pod's `endpoints`"),
    (_api_error(400, "Endpoint Not Found", "We are unable to find the endpoint you requested."), "port_not_found",
     "Use a port number from the pod's `endpoints`"),
])
async def test_lock_refusals_from_the_server_say_what_to_do(mcp_client, fake, with_key, error, code, phrase):
    _setup(fake, raise_on={"set_port_lock": error})
    result, body = await _call(mcp_client, "set_port_lock", {"pod": POD, "port": 8188, "locked": True,
                                                             "operation_id": OP})
    assert result.is_error and body["code"] == code and phrase in body["next_step"]


async def test_locking_while_the_platform_has_locks_turned_off(mcp_client, fake, with_key):
    """What the SDK raises for the port routes' 404 `endpoint_lock_disabled`. There is no open-port way out here."""
    off = NotFoundError(404, "Port locking is turned off on this Meshive server right now.", title="Not Found",
                        raw={"detail": {"title": "Not Found", "message": "Endpoint lock is not available.",
                                        "code": "endpoint_lock_disabled"}})
    _setup(fake, raise_on={"set_port_lock": off})
    result, body = await _call(mcp_client, "set_port_lock", {"pod": POD, "port": 8188, "locked": True,
                                                             "operation_id": OP})
    assert result.is_error and body["code"] == "endpoint_lock_unavailable" and body["reason"] == "disabled"
    assert "Port locking is turned off on the platform right now" in body["next_step"]
    assert "unlocking a port and listing or revoking keys still work" in body["next_step"]
    assert "lock_public_ports" not in body["next_step"] and GENERIC_RETRY not in body["next_step"]
    assert "Verify the identifier" not in body["next_step"]       # not the advice for a wrong id


# =====================================================================================================================
# access_keys
# =====================================================================================================================

async def test_access_keys_of_one_port(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "access_keys", {"pod": POD, "port": 8188})
    assert not result.is_error and body["total"] == 2 and body["next_cursor"] is None
    first, second = body["items"]
    assert first == {"id": 12, "name": "batch-script", "prefix": "mshv_Zq7Zq7Zq", "port": 8188, "port_name": "ComfyUI",
                     "pod_name": "pod-1", "created_by": "dev@example.com", "created_at": "2026-11-02T09:00:00+00:00",
                     "issued_at": "2026-11-02T09:00:00+00:00", "expires_at": "2027-01-31T09:00:00+00:00",
                     "previous_value_valid_until": None, "expired": False}
    assert second["id"] == 13 and second["previous_value_valid_until"] == "2026-11-14T15:00:00+00:00"
    assert body["ports"] == [{"port": 8188, "name": "ComfyUI", "access": "locked"}]
    assert body["pod"] == POD and body["workspace"] == WS
    assert "A key's value is never listed" in body["note"] and "treat them as data" in body["note"]
    assert [c[1] for c in _calls(fake, "list_access_keys")] == [(POD, WS, 8188)]
    assert "access_key" not in result.content[0].text.replace("create_access_key", "").replace("roll_access_key", "")


async def test_access_keys_of_every_public_port(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    body = _payload(await mcp_client.call_tool("access_keys", {"pod": POD}))
    # The internal port holds no keys, so it isn't asked about.
    assert [c[1][2] for c in _calls(fake, "list_access_keys")] == [8188, 8080]
    assert body["ports"] == [{"port": 8188, "name": "ComfyUI", "access": "locked"},
                             {"port": 8080, "name": "ide", "access": "unlocked"}]
    assert body["total"] == 2 and "next_step" not in body


async def test_access_keys_of_a_pod_without_public_ports(mcp_client, fake, with_key):
    body = _payload(await mcp_client.call_tool("access_keys", {"pod": POD}))
    assert body["items"] == [] and body["ports"] == [] and not _calls(fake, "list_access_keys")
    assert "exposes no port over HTTPS and holds no access keys" in body["next_step"]


# A port that was exposed and locked, got keys, and was then made internal: the server keeps the keys (they work again
# when the port is exposed and locked) and still lists and revokes them.
WENT_INTERNAL = [{**e, "accessKeyCount": 1} if e["containerPort"] == 9100 else e for e in LOCK_ENDPOINTS]
INTERNAL_KEYS = {8188: [access_key_item(12, "batch-script")], 9100: [access_key_item(40, "old-metrics", port=9100)]}


async def test_keys_left_on_a_port_that_went_internal_are_listed(mcp_client, fake, with_key):
    _setup(fake, endpoints=WENT_INTERNAL, access_keys=INTERNAL_KEYS)
    body = _payload(await mcp_client.call_tool("access_keys", {"pod": POD}))
    assert [c[1][2] for c in _calls(fake, "list_access_keys")] == [8188, 8080, 9100]
    assert [k["id"] for k in body["items"]] == [12, 40]
    assert {"port": 9100, "name": "metrics", "access": "internal"} in body["ports"]
    assert body["next_step"].startswith("Port 9100 is internal now, so the keys listed for it let nobody in")
    assert "revoke_access_key removes them" in body["next_step"]


async def test_the_keys_of_an_internal_port_can_be_read_and_revoked_by_number(mcp_client, fake, with_key):
    _setup(fake, endpoints=WENT_INTERNAL, access_keys=INTERNAL_KEYS)
    result, body = await _call(mcp_client, "access_keys", {"pod": POD, "port": 9100})
    assert not result.is_error and [k["id"] for k in body["items"]] == [40]
    assert body["ports"] == [{"port": 9100, "name": "metrics", "access": "internal"}]
    assert "Port 9100 is internal now" in body["next_step"]
    # The key found there can be revoked through this server.
    preview = _payload(await mcp_client.call_tool("revoke_access_key", {"pod": POD, "port": 9100, "key_id": 40}))
    assert preview["confirmed"] is False and preview["key"]["name"] == "old-metrics"
    await mcp_client.call_tool("revoke_access_key", {"pod": POD, "port": 9100, "key_id": 40, "confirm": True,
                                                     "operation_id": preview["operation_id"]})
    assert _calls(fake, "revoke_access_key")[-1][1] == (POD, WS, 9100, 40)


async def test_an_internal_port_without_keys_lists_nothing_and_says_nothing_more(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "access_keys", {"pod": POD, "port": 9100})
    assert not result.is_error and body["items"] == [] and "next_step" not in body
    assert body["ports"] == [{"port": 9100, "name": "metrics", "access": "internal"}]


async def test_access_keys_of_a_port_the_pod_does_not_open_are_refused(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "access_keys", {"pod": POD, "port": 1})
    assert result.is_error and body["code"] == "port_not_found"
    assert not _calls(fake, "list_access_keys")


async def test_access_keys_pages_like_other_lists(mcp_client, fake, with_key):
    keys = [access_key_item(100 + i, f"k{i}") for i in range(5)]
    _setup(fake, endpoints=LOCK_ENDPOINTS, access_keys={8188: keys})
    first = _payload(await mcp_client.call_tool("access_keys", {"pod": POD, "port": 8188, "limit": 2}))
    assert [k["id"] for k in first["items"]] == [100, 101] and first["total"] == 5 and first["next_cursor"]
    rest = _payload(await mcp_client.call_tool("access_keys", {"pod": POD, "port": 8188, "limit": 10,
                                                               "cursor": first["next_cursor"]}))
    assert [k["id"] for k in rest["items"]] == [102, 103, 104] and rest["next_cursor"] is None


# =====================================================================================================================
# create_access_key
# =====================================================================================================================

KEY_ARGS = {"pod": POD, "port": 8188, "name": "ci"}


async def test_creating_a_key_is_previewed_and_creates_nothing(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "create_access_key", KEY_ARGS)
    assert not result.is_error and body["confirmed"] is False and body["action"] == "create_access_key"
    assert not _calls(fake, "create_access_key")
    assert body["name"] == "ci" and body["port"] == 8188 and body["expires_in_days"] == 90
    assert body["question"].startswith(f"Create access key 'ci' for port 8188 of pod '{POD}'? It expires in 90 days.")
    assert "Any program that has its value can call this port until the key expires or is revoked." in body["question"]
    assert "a secret for the user alone and is shown only once" in body["question"]
    assert body["next_step"].endswith("Reuse this operation_id for the confirmation.")
    assert "mshv_" not in result.content[0].text            # a preview never carries a secret


NO_SECOND_CALL = ("If the confirmed call ends without a clear answer (a timeout on your side included), don't call it "
                  "again, not with this operation_id either: read access_keys first. The server keeps no record of "
                  "this call for operation_status to find, and another call issues another key value.")


@pytest.mark.parametrize("tool,args", [("create_access_key", {"pod": POD, "port": 8188, "name": "ci"}),
                                       ("roll_access_key", {"pod": POD, "port": 8188, "key_id": 12})])
async def test_a_key_preview_says_what_to_do_if_the_confirmed_call_gives_no_answer(mcp_client, fake, with_key, tool,
                                                                                  args):
    """The agent may never see this server's error for a lost call (its own client can time out first). The general
    rule for writes, to reuse the operation_id and ask operation_status, would then issue a second value, so the
    preview, which the agent has in hand before the confirmed call, says what holds for this call instead."""
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    step = _payload(await mcp_client.call_tool(tool, args))["next_step"]
    assert NO_SECOND_CALL in step
    assert "Never set confirm=true on your own." in step
    # The id is for the confirmation only; nothing here says to reuse it for a retry.
    assert step.endswith("Reuse this operation_id for the confirmation.") and "every retry" not in step


async def test_other_previews_keep_the_general_retry_rule(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    for tool, args in (("revoke_access_key", {"pod": POD, "port": 8188, "key_id": 12}),
                       ("set_port_lock", {"pod": POD, "port": 8188, "locked": False})):
        step = _payload(await mcp_client.call_tool(tool, args))["next_step"]
        assert "don't call it again" not in step and step.endswith("for confirmation and every retry."), tool


async def test_the_confirmed_answer_carries_the_value_once(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "create_access_key", {**KEY_ARGS, "confirm": True, "operation_id": OP})
    assert not result.is_error
    (_, args, kw), = _calls(fake, "create_access_key")
    # The operation_id isn't passed on as the Idempotency-Key: the server keeps no replayable copy of an answer that
    # holds the value.
    assert args == (POD, WS, 8188, "ci") and kw == {"expires_in_days": 90}
    assert body["access_key"] == ACCESS_KEY
    assert body["key"] == {"id": 21, "name": "ci", "prefix": "mshv_Zq7Zq7Zq", "port": 8188, "port_name": "ComfyUI",
                           "pod_name": "pod-1", "created_by": "dev@example.com",
                           "created_at": "2026-11-02T09:00:00+00:00", "issued_at": "2026-11-02T09:00:00+00:00",
                           "expires_at": "2027-02-14T09:00:00+00:00", "previous_value_valid_until": None,
                           "expired": False}
    # Exactly one copy in the text and one in the structured answer: `access_key`, and nowhere else.
    assert result.content[0].text.count(ACCESS_KEY) == 1
    assert json.dumps(result.structured_content).count(ACCESS_KEY) == 1
    assert result.structured_content["access_key"] == ACCESS_KEY
    assert ACCESS_KEY not in json.dumps({k: v for k, v in body.items() if k != "access_key"})
    assert "It is in this answer only and can't be read again." in body["note"]
    assert "never use it yourself, and never put it in files, commits, commands or other tools" in body["note"]
    assert "Give `access_key` to the user who asked, now" in body["next_step"]
    assert "`Authorization: Bearer <key>`, or as `X-Meshive-Key: <key>`" in body["next_step"]
    assert "operation_lookup" not in body


async def test_key_lifetimes(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    never = _payload(await mcp_client.call_tool("create_access_key", {**KEY_ARGS, "expires_in_days": 0}))
    assert "It never expires." in never["question"]
    done = _payload(await mcp_client.call_tool("create_access_key", {**KEY_ARGS, "expires_in_days": 0,
                                                                     "confirm": True, "operation_id": OP}))
    assert _calls(fake, "create_access_key")[-1][2] == {"expires_in_days": 0} and done["key"]["expires_at"] is None
    for bad in (7, 1, -1, 3650):
        result, body = await _call(mcp_client, "create_access_key", {**KEY_ARGS, "expires_in_days": bad,
                                                                     "confirm": True, "operation_id": OP})
        assert result.is_error and body["code"] == "invalid_argument" and "30, 90 or 365, or 0" in body["message"], bad
    assert len(_calls(fake, "create_access_key")) == 1          # the refused ones were never sent


async def test_a_key_needs_a_name(mcp_client, fake, with_key):
    result, body = await _call(mcp_client, "create_access_key", {**KEY_ARGS, "name": "   ", "confirm": True,
                                                                 "operation_id": OP})
    assert result.is_error and body["code"] == "invalid_argument" and "last" not in fake


async def test_a_key_name_is_at_most_64_characters(mcp_client, fake, with_key):
    """Refused here in plain words; the server's own refusal of a long name is a validation dump."""
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "create_access_key", {**KEY_ARGS, "name": "n" * 65, "confirm": True,
                                                                 "operation_id": OP})
    assert result.is_error and body["code"] == "invalid_argument" and "1-64 characters" in body["message"]
    assert "last" not in fake
    ok = await mcp_client.call_tool("create_access_key", {**KEY_ARGS, "name": " " + "n" * 64 + " ", "confirm": True,
                                                          "operation_id": OP})
    assert not ok.is_error and _calls(fake, "create_access_key")[-1][1][3] == "n" * 64


async def test_a_key_is_only_for_a_locked_port(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "create_access_key", {**KEY_ARGS, "port": 8080})
    assert result.is_error and body["code"] == "port_not_locked" and "Port 8080 is unlocked." == body["message"]
    assert "Ask the user whether to lock it (set_port_lock with locked=true; no restart)" in body["next_step"]
    result, body = await _call(mcp_client, "create_access_key", {**KEY_ARGS, "port": 9100})
    assert result.is_error and body["code"] == "port_not_public"
    assert not _calls(fake, "create_access_key")


@pytest.mark.parametrize("error,code,phrase", [
    (_api_error(400, "Endpoint Not Locked", "Access keys can be created only for a locked port. Lock the port first.",
                code="endpoint_not_locked"), "port_not_locked", "Ask the user whether to lock it"),
    (_api_error(400, "Too Many Keys", "A port can have up to 20 access keys.", code="too_many_access_keys"),
     "too_many_access_keys", "ask which one to revoke"),
    (PermissionDeniedError(403, "Only workspace admins can do this.", title="Forbidden"), "forbidden",
     "Tell the user and do not retry"),
    (RateLimitError(429, "Too many requests", retry_after=7), "rate_limited", "Wait at least 7s"),
])
async def test_key_refusals_from_the_server_say_what_to_do(mcp_client, fake, with_key, error, code, phrase):
    """A clear refusal: nothing was created, and the advice is the refusal's own."""
    _setup(fake, endpoints=LOCK_ENDPOINTS, raise_on={"create_access_key": error})
    result, body = await _call(mcp_client, "create_access_key", {**KEY_ARGS, "confirm": True, "operation_id": OP})
    assert result.is_error and body["code"] == code and phrase in body["next_step"]
    assert "operation_lookup" not in body and GENERIC_RETRY not in body["next_step"]


@pytest.mark.parametrize("tool,method,args,check", [
    # Names aren't unique: the new key is the one issued just now, not an older key called the same.
    ("create_access_key", "create_access_key", KEY_ARGS,
     "If a key named 'ci' whose `issued_at` is from just now is listed (an older key can have the same name), it was "
     "created, but its value was lost"),
    ("roll_access_key", "roll_access_key", {"pod": POD, "port": 8188, "key_id": 12},
     "If key 12 is still listed, nothing changed and the user can ask again."),
])
@pytest.mark.parametrize("error", [httpx.ReadTimeout("timed out"), httpx.RemoteProtocolError("closed"),
                                   MeshiveAPIError(500, "boom"), MeshiveAPIError(502, "bad gateway"),
                                   MeshiveAPIError(503, "unavailable"), httpx.ConnectError("refused")],
                         ids=lambda e: f"{type(e).__name__}-{getattr(e, 'status_code', '')}")
async def test_an_unclear_answer_is_followed_by_the_key_list_not_a_second_call(mcp_client, fake, with_key, tool,
                                                                              method, args, check, error):
    """Every call issues another value and the SDK sends none again, so neither does the agent: it reads the list."""
    _setup(fake, endpoints=LOCK_ENDPOINTS, raise_on={method: error})
    result, body = await _call(mcp_client, tool, {**args, "confirm": True, "operation_id": OP})
    assert result.is_error and body["code"] == "access_key_outcome_unknown"
    assert "may or may not have gone through" in body["message"]
    step = body["next_step"]
    assert step.startswith("Don't call this tool again yet: another call would issue another key value. First read "
                           "the port's keys with access_keys.")
    assert check in step
    # No advice that reads as "just retry": not the generic one, not the one for unrecorded calls, no operation_status.
    assert GENERIC_RETRY not in step and UNRECORDED_RETRY not in step and "operation_status" not in step
    assert "retry_after" not in body and "operation_lookup" not in body
    assert len(_calls(fake, method)) == 1                    # the tool itself didn't call again either
    assert "mshv_" not in result.content[0].text


async def test_an_answer_without_a_value_is_an_error(mcp_client, fake, with_key):
    from meshive.models import AccessKeyCreated

    async def no_value(self, pod, ws, port, name, *, expires_in_days=90):
        return AccessKeyCreated.from_dict(access_key_item(21, name, port=port))

    _setup(fake, endpoints=LOCK_ENDPOINTS)
    import conftest
    original = conftest.FakeMeshive.create_access_key
    conftest.FakeMeshive.create_access_key = no_value
    try:
        result, body = await _call(mcp_client, "create_access_key", {**KEY_ARGS, "confirm": True,
                                                                     "operation_id": OP})
    finally:
        conftest.FakeMeshive.create_access_key = original
    assert result.is_error and body["code"] == "access_key_value_missing" and body["key"]["id"] == 21
    assert "revoke_access_key" in body["next_step"] and UNRECORDED_RETRY not in body["next_step"]
    assert "roll" not in body["next_step"]


async def test_a_roll_whose_answer_lost_the_value_is_rolled_again_not_replaced(mcp_client, fake, with_key):
    """The roll went through: the key has its new id and the old value is running out (or gone). Rolling once more
    keeps the key's name and lifetime; revoking it and creating another would not."""
    from meshive.models import AccessKeyCreated

    async def no_value(self, pod, ws, port, key_id, *, overlap_seconds=3600):
        return AccessKeyCreated.from_dict(access_key_item(31, "batch-script", port=port,
                                                          previous_until="2026-11-16T10:00:00Z"))

    _setup(fake, endpoints=LOCK_ENDPOINTS)
    import conftest
    original = conftest.FakeMeshive.roll_access_key
    conftest.FakeMeshive.roll_access_key = no_value
    try:
        result, body = await _call(mcp_client, "roll_access_key", {**ROLL_ARGS, "confirm": True, "operation_id": OP})
    finally:
        conftest.FakeMeshive.roll_access_key = original
    assert result.is_error and body["code"] == "access_key_value_missing" and body["key"]["id"] == 31
    step = body["next_step"]
    assert step.startswith("The key was rolled and has id 31 now, but its new value can't be read again. First read "
                           "access_keys")
    assert "`previous_value_valid_until` says until when the old value still works" in step
    assert "null means it has stopped already, so programs that send it get 401 now" in step
    assert "ask whether to roll the key again under its new id (roll_access_key)" in step
    assert "revoke" not in step and UNRECORDED_RETRY not in step


# =====================================================================================================================
# roll_access_key
# =====================================================================================================================

ROLL_ARGS = {"pod": POD, "port": 8188, "key_id": 12}


async def test_rolling_a_key_is_previewed_with_the_key_it_is_about(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "roll_access_key", ROLL_ARGS)
    assert not result.is_error and body["confirmed"] is False and not _calls(fake, "roll_access_key")
    assert body["key"]["id"] == 12 and body["key"]["name"] == "batch-script" and body["overlap_seconds"] == 3600
    assert body["question"].startswith(f"Roll access key 'batch-script' (mshv_Zq7Zq7Zq…) of port 8188 of pod '{POD}'?")
    assert "the old value stops working in 1 hour: programs still sending the old value then get 401" in body["question"]
    assert "mshv_Zq7Zq7Zq" in result.content[0].text and ACCESS_KEY not in result.content[0].text


@pytest.mark.parametrize("overlap,when", [(0, "at once"), (86400, "in 24 hours"), (604800, "in 7 days")])
async def test_roll_overlaps(mcp_client, fake, with_key, overlap, when):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    body = _payload(await mcp_client.call_tool("roll_access_key", {**ROLL_ARGS, "overlap_seconds": overlap}))
    assert f"the old value stops working {when}" in body["question"]


async def test_roll_refuses_what_it_cannot_do_before_anything_is_sent(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "roll_access_key", {**ROLL_ARGS, "overlap_seconds": 60})
    assert result.is_error and body["code"] == "invalid_argument" and "0, 3600 (1 hour)" in body["message"]
    result, body = await _call(mcp_client, "roll_access_key", {**ROLL_ARGS, "key_id": 99})
    assert result.is_error and body["code"] == "access_key_not_found"
    assert [k["id"] for k in body["keys"]] == [12, 13] and "a roll gives the key a new id" in body["next_step"]
    result, body = await _call(mcp_client, "roll_access_key", {**ROLL_ARGS, "port": 8080})
    assert result.is_error and body["code"] == "port_not_locked"
    assert not _calls(fake, "roll_access_key")


async def test_the_rolled_value_is_returned_once_with_the_old_values_deadline(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "roll_access_key", {**ROLL_ARGS, "confirm": True, "operation_id": OP})
    assert not result.is_error
    (_, args, kw), = _calls(fake, "roll_access_key")
    assert args == (POD, WS, 8188, 12) and kw == {"overlap_seconds": 3600}
    assert body["access_key"] == ROLLED_ACCESS_KEY and result.content[0].text.count(ROLLED_ACCESS_KEY) == 1
    assert json.dumps(result.structured_content).count(ROLLED_ACCESS_KEY) == 1
    assert body["key"]["id"] == 31 and body["key"]["previous_value_valid_until"] == "2026-11-16T10:00:00+00:00"
    assert "The old value works until 2026-11-16T10:00:00+00:00" in body["next_step"]
    assert "The key's id is 31 from now on." in body["next_step"]
    assert "never use it yourself" in body["note"]

    at_once = _payload(await mcp_client.call_tool("roll_access_key", {**ROLL_ARGS, "overlap_seconds": 0,
                                                                      "confirm": True, "operation_id": OP}))
    assert "The old value no longer works" in at_once["next_step"]


async def test_an_unclear_roll_says_how_to_tell_whether_it_went_through(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS, raise_on={"roll_access_key": httpx.ReadTimeout("timed out")})
    result, body = await _call(mcp_client, "roll_access_key", {**ROLL_ARGS, "confirm": True, "operation_id": OP})
    assert body["code"] == "access_key_outcome_unknown"
    assert "a new `id` and a fresh `issued_at` is listed, the roll went through" in body["next_step"]
    assert ("the old value works until that key's `previous_value_valid_until`. Tell the user, and ask whether to roll "
            "the key again under its new id.") in body["next_step"]
    assert "stopped working at once" not in body["next_step"]


async def test_an_unclear_roll_without_overlap_says_the_old_value_may_be_dead_already(mcp_client, fake, with_key):
    """With overlap_seconds=0 a roll that went through has cut the old value off: the user's programs get 401 now."""
    _setup(fake, endpoints=LOCK_ENDPOINTS, raise_on={"roll_access_key": httpx.ReadTimeout("timed out")})
    result, body = await _call(mcp_client, "roll_access_key", {**ROLL_ARGS, "overlap_seconds": 0, "confirm": True,
                                                               "operation_id": OP})
    assert body["code"] == "access_key_outcome_unknown"
    step = body["next_step"]
    assert "If key 12 is still listed, nothing changed and the user can ask again." in step
    assert ("the old value stopped working at once (overlap_seconds=0), so programs that still send it get 401 now. "
            "Tell the user right away, and ask whether to roll the key again under its new id.") in step
    assert "works until that key's `previous_value_valid_until`" not in step


# =====================================================================================================================
# revoke_access_key
# =====================================================================================================================

async def test_revoking_a_key_is_previewed_then_sent_with_the_operation_id(mcp_client, fake, with_key):
    args = {"pod": POD, "port": 8188, "key_id": 13}
    result, body = await _call(mcp_client, "revoke_access_key", args)
    assert not result.is_error and body["confirmed"] is False and not _calls(fake, "revoke_access_key")
    assert body["key"]["id"] == 13 and body["key"]["name"] == "ci"
    assert body["question"] == ("Revoke access key 'ci' (mshv_Zq7Zq7Zq…) of port 8188? Its value stops working within "
                                "seconds and programs that send it get 401. This cannot be undone.")
    done = _payload(await mcp_client.call_tool("revoke_access_key", {**args, "confirm": True,
                                                                     "operation_id": body["operation_id"]}))
    (_, sent, kw), = _calls(fake, "revoke_access_key")
    assert sent == (POD, WS, 8188, 13) and kw == {"idempotency_key": body["operation_id"]}
    assert done["action"] == "revoke" and done["id"] == 13 and done["port"] == 8188
    assert "A connection that was already open stays open until it closes." in done["next_step"]


async def test_revoking_an_unknown_key(mcp_client, fake, with_key):
    result, body = await _call(mcp_client, "revoke_access_key", {"pod": POD, "port": 8188, "key_id": 99})
    assert result.is_error and body["code"] == "access_key_not_found" and not _calls(fake, "revoke_access_key")
    # The same refusal from the server, when the key went away between the preview and the confirmation.
    _setup(fake, raise_on={"revoke_access_key": NotFoundError(404, "Access key not found.", title="Not Found")})
    result, body = await _call(mcp_client, "revoke_access_key", {"pod": POD, "port": 8188, "key_id": 12,
                                                                 "confirm": True, "operation_id": OP})
    assert result.is_error and body["code"] == "access_key_not_found" and "access_keys" in body["next_step"]


# =====================================================================================================================
# Only a workspace admin or billing member changes locks and keys
# =====================================================================================================================

@pytest.mark.parametrize("tool,method,args", [
    ("set_port_lock", "set_port_lock", {"pod": POD, "port": 8188, "locked": True}),
    ("set_port_lock", "set_port_lock", {"pod": POD, "port": 8188, "locked": False}),
    ("create_access_key", "create_access_key", KEY_ARGS),
    ("roll_access_key", "roll_access_key", ROLL_ARGS),
    ("revoke_access_key", "revoke_access_key", {"pod": POD, "port": 8188, "key_id": 12}),
])
async def test_a_member_without_the_role_is_told_so_and_nothing_is_retried(mcp_client, fake, with_key, tool, method,
                                                                          args):
    denied = PermissionDeniedError(403, "Only a workspace admin or billing member can do this.", title="Forbidden")
    _setup(fake, endpoints=LOCK_ENDPOINTS, raise_on={method: denied})
    result, body = await _call(mcp_client, tool, {**args, "confirm": True, "operation_id": OP})
    assert result.is_error and body["code"] == "forbidden"
    assert "not an admin of the workspace" in body["next_step"] and "do not retry" in body["next_step"]
    assert len(_calls(fake, method)) == 1 and "mshv_Zq7Zq7Zq7" not in result.content[0].text


async def test_a_read_only_key_cannot_change_a_lock(mcp_client, fake, with_key):
    _setup(fake, raise_on={"set_port_lock": PermissionDeniedError(403, "This API key lacks the 'write' scope.")})
    result, body = await _call(mcp_client, "set_port_lock", {"pod": POD, "port": 8188, "locked": True,
                                                             "operation_id": OP})
    assert result.is_error and body["code"] == "write_scope_required"


# =====================================================================================================================
# A key's value stays in the one answer that issued it
# =====================================================================================================================

async def test_key_values_appear_nowhere_but_in_the_answer_that_issued_them(mcp_client, fake, with_key, caplog):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    caplog.set_level(logging.DEBUG)
    created = await mcp_client.call_tool("create_access_key", {**KEY_ARGS, "confirm": True, "operation_id": OP})
    rolled = await mcp_client.call_tool("roll_access_key", {**ROLL_ARGS, "confirm": True, "operation_id": OP})
    assert ACCESS_KEY in created.content[0].text and ROLLED_ACCESS_KEY in rolled.content[0].text
    assert ROLLED_ACCESS_KEY not in created.content[0].text and ACCESS_KEY not in rolled.content[0].text
    others = [
        await mcp_client.call_tool("create_access_key", KEY_ARGS),
        await mcp_client.call_tool("roll_access_key", ROLL_ARGS),
        await mcp_client.call_tool("revoke_access_key", {"pod": POD, "port": 8188, "key_id": 12}),
        await mcp_client.call_tool("revoke_access_key", {"pod": POD, "port": 8188, "key_id": 12, "confirm": True,
                                                         "operation_id": OP}),
        await mcp_client.call_tool("access_keys", {"pod": POD}),
        await mcp_client.call_tool("access_keys", {"pod": POD, "port": 8188}),
        await mcp_client.call_tool("pods", {"pod": POD, "workspace": WS, "show_secrets": True}),
        await mcp_client.call_tool("pods", {}),
        await mcp_client.call_tool("set_port_lock", {"pod": POD, "port": 8188, "locked": False}),
        await mcp_client.call_tool("set_port_lock", {"pod": POD, "port": 8188, "locked": True, "operation_id": OP}),
    ]
    for result in others:
        text = result.content[0].text + json.dumps(result.structured_content or {})
        assert "mshv_Zq7Zq7Zq7" not in text and "mshv_Rk2" not in text, text[:200]
    # Nothing this server logs, at any level, holds a value.
    logged = "\n".join(record.getMessage() + repr(record.args) for record in caplog.records)
    assert ACCESS_KEY not in logged and ROLLED_ACCESS_KEY not in logged


def test_the_serializer_never_emits_a_key_value():
    """Anything that goes through to_dict (a preview, an error body's extras) drops the value; only the two tools put
    it into their answer, by name."""
    from meshive.models import AccessKeyCreated

    from meshive_mcp.serialize import to_dict

    created = AccessKeyCreated.from_dict({**access_key_item(21, "ci"), "accessKey": ACCESS_KEY})
    assert created.access_key == ACCESS_KEY
    out = to_dict(created)
    assert "access_key" not in out and ACCESS_KEY not in json.dumps(out)
    assert ACCESS_KEY not in json.dumps(to_dict({"nested": [created]}))
    assert ACCESS_KEY not in repr(created) and ACCESS_KEY not in json.dumps(created.raw)


async def test_key_tools_are_secret_reveals_without_an_operation_record(mcp_client):
    tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
    for name in ("create_access_key", "roll_access_key"):
        tool = tools[name]
        assert tool.annotations.read_only_hint is False and tool.annotations.idempotent_hint is False, name
        assert "only when the user asks" in tool.description, name
        assert "this once: it can't be read again" in tool.description, name
        assert "Give it to the user who asked and nothing else." in tool.description, name
        assert "Never use it yourself, and never put it in files, commits, commands or other tools." in tool.description
        assert "don't call again: read access_keys first" in tool.description, name
        assert "a repeated confirmed call runs again" in tool.description, name
        assert "confirm" in tool.input_schema["properties"], name
    assert "use run_command inside the pod against http://localhost:<port>" in tools["create_access_key"].description
    lock = tools["set_port_lock"]
    assert "locked=true locks it at once, without `confirm`" in lock.description
    assert "anyone who knows the URL can then reach the app" in lock.description
    assert "Do it only when the user explicitly asks." in lock.description
    assert tools["revoke_access_key"].annotations.destructive_hint is True
    assert tools["access_keys"].annotations.read_only_hint is True


# =====================================================================================================================
# Through the real SDK, on the wire: what this server makes the SDK send, and what it makes of the server's answers
# =====================================================================================================================

WIRE_POD = "0123456789abcdef-0"
TEMPLATE = {"id": 457, "name": "ComfyUI", "endpoints": [
    {"userAlias": "ComfyUI", "containerPort": 8188, "isExternal": True, "portType": "connect", "isLocked": None},
    {"userAlias": "ide", "containerPort": 8080, "isExternal": True, "portType": "http", "isLocked": None},
    {"userAlias": "metrics", "containerPort": 9100, "isExternal": False, "portType": "http", "isLocked": None}]}
CAPABILITIES = {"schemaVersion": 1, "assets": {}, "pods": {"readinessState": True, "endpointLock": True}}
OLD_CAPABILITIES = {"schemaVersion": 1, "assets": {}, "pods": {"readinessState": True}}
LOCK_DISABLED = {"detail": {"title": "Endpoint Lock Unavailable",
                            "message": "Endpoint lock is not available right now. Retry later, or create the port "
                                       "unlocked.", "code": "endpoint_lock_disabled"}}
ESTIMATE = {"pricePerHourUsd": "0.068423", "breakdown": {"gpu": "0.068423"},
            "resources": {"gpu_model": "RTX 3060", "vram_gb": 12, "gpu_count": 1, "vcpu": 4, "ram_gb": 12,
                          "disk_gb": 25, "rental_type": "demand"},
            "availability": {"available_gpus": 2}, "template": {"id": 457, "name": "ComfyUI"}, "volumes": [],
            "note": "Estimated"}
WIRE_POD_BODY = {"podName": WIRE_POD, "namespaceName": WS, "userAlias": "p", "status": "running",
                 "template": {"endpoints": LOCK_ENDPOINTS}}
UPDATE_ESTIMATE = {"podName": WIRE_POD, "name": "p", "changes": ["ports: open 9000"], "restarts": True,
                   "pricePerHourUsd": "0.068", "currentPricePerHourUsd": "0.068"}
POD_PATH = f"/v1/sdk/pods/{WIRE_POD}"
KEYS_PATH = f"{POD_PATH}/ports/8188/keys"


class Api:
    """MockTransport handler standing in for the Meshive API: one answer per (method, path), every request kept."""

    def __init__(self):
        self.routes = {("GET", "/v1/sdk/templates/457"): (200, TEMPLATE), ("GET", "/v1/capabilities"): (200, CAPABILITIES),
                       ("POST", "/v1/sdk/pods/estimate"): (200, ESTIMATE),
                       ("POST", "/v1/sdk/pods"): (202, {"name": "p", "workspace": WS, "transactionId": 8,
                                                        "estimate": ESTIMATE}),
                       ("GET", POD_PATH): (200, WIRE_POD_BODY),
                       ("POST", f"{POD_PATH}/estimate"): (200, UPDATE_ESTIMATE),
                       ("PATCH", POD_PATH): (202, {"podName": WIRE_POD, "workspace": WS, "accepted": True,
                                                   "transactionId": 9}),
                       ("PATCH", f"{POD_PATH}/ports/8188/lock"): (200, {"port": 8188, "locked": False}),
                       ("GET", KEYS_PATH): (200, {"keys": [access_key_item(12, "batch-script")]}),
                       ("POST", KEYS_PATH): (201, {**access_key_item(21, "ci"), "accessKey": ACCESS_KEY}),
                       ("DELETE", f"{KEYS_PATH}/12"): (200, {"title": "Success", "message": "Access key revoked."})}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get((request.method, request.url.path))
        if answer is None:
            raise AssertionError(f"unexpected {request.method} {request.url}")
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer[0], json=answer[1])

    def sent(self, method, path):
        return [r for r in self.requests if (r.method, r.url.path) == (method, path)]

    def body(self, method, path, index=-1):
        return json.loads(self.sent(method, path)[index].content)


def _locks(body):
    """{port: locked} for every port a request body carries a lock value for."""
    found = {e["port"]: e["locked"] for e in body.get("ports", []) if "locked" in e}
    found.update({e["port"]: e["locked"] for e in body.get("templatePorts", []) if "locked" in e})
    return found


@pytest.fixture
def wire(monkeypatch, with_key):
    """The tools over the real SDK client, with the API replaced at the transport: no network, the real requests."""
    from meshive_mcp import client as client_module

    api = Api()
    real = client_module._new_client

    def factory(key, label):
        client = real(key, label)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(api))
        return client

    monkeypatch.setattr(client_module, "_new_client", factory)
    return api


WIRE_CREATE = {**CREATE, "workspace": WS}


async def test_wire_a_plain_create_locks_every_public_port(mcp_client, wire):
    result, body = await _call(mcp_client, "create_pod", WIRE_CREATE)
    assert not result.is_error, result.content
    # The estimate already carries the locks the create would send.
    assert _locks(wire.body("POST", "/v1/sdk/pods/estimate")) == {8188: True, 8080: True}
    assert body["locked_ports"] == [8080, 8188] and body["open_ports"] == []
    assert "Its 2 public ports (8080 and 8188) are created locked." in body["question"]
    assert not wire.sent("POST", "/v1/sdk/pods")

    done = _payload(await mcp_client.call_tool("create_pod", {**WIRE_CREATE, "confirm": True,
                                                              "operation_id": body["operation_id"]}))
    sent = wire.body("POST", "/v1/sdk/pods")
    assert _locks(sent) == {8188: True, 8080: True}
    assert all("locked" not in e for e in sent.get("templatePorts", []) if e["port"] == 9100)   # internal: no lock
    assert wire.sent("POST", "/v1/sdk/pods")[0].headers["Idempotency-Key"] == body["operation_id"]
    assert done["locked_ports"] == [8080, 8188] and "Ports 8080 and 8188 are locked." in done["next_step"]


async def test_wire_new_ports_are_locked_and_internal_ones_carry_no_lock(mcp_client, wire):
    await mcp_client.call_tool("estimate_pod", {**WIRE_CREATE, "ports": [7000, {"port": 7001, "external": False}]})
    sent = wire.body("POST", "/v1/sdk/pods/estimate")
    assert sent["ports"] == [{"port": 7000, "external": True, "locked": True}, {"port": 7001, "external": False}]


async def test_wire_open_ports_only_when_asked(mcp_client, wire):
    result, body = await _call(mcp_client, "create_pod", {**WIRE_CREATE, "lock_public_ports": False})
    assert _locks(wire.body("POST", "/v1/sdk/pods/estimate")) == {8188: False, 8080: False}
    assert body["open_ports"] == [8080, 8188] and body["locked_ports"] == []
    assert "WARNING: ports 8080 and 8188 will be OPEN (unlocked)" in body["question"]
    assert not wire.sent("POST", "/v1/sdk/pods")              # still only a preview

    one = _payload(await mcp_client.call_tool("create_pod", {**WIRE_CREATE,
                                                             "template_ports": [{"port": 8080, "locked": False}]}))
    assert _locks(wire.body("POST", "/v1/sdk/pods/estimate")) == {8188: True, 8080: False}
    assert one["locked_ports"] == [8188] and one["open_ports"] == [8080]


@pytest.mark.parametrize("confirm", [False, True])
async def test_wire_a_server_without_port_locks_gets_no_request_at_all(mcp_client, wire, confirm):
    wire.routes[("GET", "/v1/capabilities")] = (200, OLD_CAPABILITIES)
    args = {**WIRE_CREATE, **({"confirm": True, "operation_id": OP} if confirm else {})}
    result, body = await _call(mcp_client, "create_pod", args)
    assert result.is_error and body["code"] == "endpoint_lock_unavailable" and body["reason"] == "unsupported"
    assert "lock_public_ports=false" in body["next_step"] and "lock_public_ports=False" not in result.content[0].text
    # Nothing that could create a pod, open or not, went out.
    assert not wire.sent("POST", "/v1/sdk/pods") and not wire.sent("POST", "/v1/sdk/pods/estimate")


async def test_wire_a_server_with_locking_turned_off_is_not_retried_open(mcp_client, wire):
    wire.routes[("POST", "/v1/sdk/pods")] = (409, LOCK_DISABLED)
    result, body = await _call(mcp_client, "create_pod", {**WIRE_CREATE, "confirm": True, "operation_id": OP})
    assert result.is_error and body["code"] == "endpoint_lock_unavailable" and body["reason"] == "disabled"
    assert "can't be locked right now" in body["next_step"]
    assert "create the port unlocked" not in result.content[0].text          # the server's wording stays out
    # One request, with the locks; no second one with the ports open.
    assert len(wire.sent("POST", "/v1/sdk/pods")) == 1
    assert _locks(wire.body("POST", "/v1/sdk/pods")) == {8188: True, 8080: True}


async def test_wire_an_edit_locks_what_it_newly_exposes_and_leaves_public_ports_alone(mcp_client, wire):
    args = {"pod": WIRE_POD, "workspace": WS, "ports": [9000],
            "template_ports": [{"port": 9100, "external": True}, {"port": 8080, "name": "editor"}]}
    result, body = await _call(mcp_client, "update_pod", args)
    assert not result.is_error, result.content
    sent = wire.body("POST", f"{POD_PATH}/estimate")
    # 9000 is new and 9100 was internal: both locked. 8080 is public already (and unlocked on purpose): untouched.
    assert _locks(sent) == {9000: True, 9100: True}
    assert body["locked_ports"] == [9000, 9100] and body["open_ports"] == []
    assert "Ports 9000 and 9100 will be locked." in body["question"]

    await mcp_client.call_tool("update_pod", {**args, "confirm": True, "operation_id": body["operation_id"]})
    assert _locks(wire.body("PATCH", POD_PATH)) == {9000: True, 9100: True}


async def test_wire_an_edit_that_exposes_nothing_sends_no_lock_and_reads_nothing_extra(mcp_client, wire):
    await mcp_client.call_tool("update_pod", {"pod": WIRE_POD, "workspace": WS, "env": {"A": "1"}})
    assert _locks(wire.body("POST", f"{POD_PATH}/estimate")) == {}
    assert [(r.method, r.url.path) for r in wire.requests] == [("POST", f"{POD_PATH}/estimate")]


async def test_wire_a_key_call_that_times_out_is_sent_once(mcp_client, wire):
    wire.routes[("POST", KEYS_PATH)] = httpx.ReadTimeout("timed out")
    result, body = await _call(mcp_client, "create_access_key", {"pod": WIRE_POD, "workspace": WS, "port": 8188,
                                                                 "name": "ci", "confirm": True, "operation_id": OP})
    assert result.is_error and body["code"] == "access_key_outcome_unknown"
    assert len(wire.sent("POST", KEYS_PATH)) == 1            # the SDK didn't resend it, and neither did the tool
    assert "First read the port's keys with access_keys." in body["next_step"]


async def test_wire_a_key_is_created_without_the_operation_id_and_returned_once(mcp_client, wire):
    result, body = await _call(mcp_client, "create_access_key", {"pod": WIRE_POD, "workspace": WS, "port": 8188,
                                                                 "name": "ci", "expires_in_days": 365,
                                                                 "confirm": True, "operation_id": OP})
    assert not result.is_error, result.content
    request, = wire.sent("POST", KEYS_PATH)
    assert json.loads(request.content) == {"name": "ci", "expiresInDays": 365}
    # The operation_id is not sent as the Idempotency-Key (the SDK puts a random one on every write, which the server
    # doesn't record for this call), so nothing ties a later call to this answer.
    assert request.headers["Idempotency-Key"] != OP and "operation_lookup" not in body
    assert body["access_key"] == ACCESS_KEY and result.content[0].text.count(ACCESS_KEY) == 1
    assert ACCESS_KEY not in json.dumps(body["key"])


async def test_wire_lock_and_revoke_carry_the_operation_id(mcp_client, wire):
    await mcp_client.call_tool("set_port_lock", {"pod": WIRE_POD, "workspace": WS, "port": 8188, "locked": False,
                                                 "confirm": True, "operation_id": OP})
    request, = wire.sent("PATCH", f"{POD_PATH}/ports/8188/lock")
    assert json.loads(request.content) == {"locked": False} and request.headers["Idempotency-Key"] == OP
    await mcp_client.call_tool("revoke_access_key", {"pod": WIRE_POD, "workspace": WS, "port": 8188, "key_id": 12,
                                                     "confirm": True, "operation_id": OP})
    request, = wire.sent("DELETE", f"{KEYS_PATH}/12")
    assert request.headers["Idempotency-Key"] == OP


@pytest.mark.parametrize("answer,reason,phrase", [
    # The API server has no such route: FastAPI's bare 404.
    ((404, {"detail": "Not Found"}), "unsupported", "has no port locks and access keys yet"),
    # The route exists and the platform has locking turned off.
    ((404, {"detail": {"title": "Not Found", "message": "Endpoint lock is not available.",
                       "code": "endpoint_lock_disabled"}}), "disabled", "turned off on the platform right now"),
])
async def test_wire_lock_calls_on_a_server_that_cannot_lock(mcp_client, wire, answer, reason, phrase):
    wire.routes[("PATCH", f"{POD_PATH}/ports/8188/lock")] = answer
    result, body = await _call(mcp_client, "set_port_lock", {"pod": WIRE_POD, "workspace": WS, "port": 8188,
                                                             "locked": True, "operation_id": OP})
    assert result.is_error and body["code"] == "endpoint_lock_unavailable" and body["reason"] == reason
    assert phrase in body["next_step"] and "Verify the identifier" not in body["next_step"]
    assert "lock_public_ports" not in body["next_step"]


async def test_wire_server_refusals_of_key_calls(mcp_client, wire):
    wire.routes[("POST", KEYS_PATH)] = (400, {"detail": {"title": "Endpoint Not Locked",
                                                         "message": "Access keys can be created only for a locked "
                                                                    "port. Lock the port first.",
                                                         "code": "endpoint_not_locked"}})
    result, body = await _call(mcp_client, "create_access_key", {"pod": WIRE_POD, "workspace": WS, "port": 8188,
                                                                 "name": "ci", "confirm": True, "operation_id": OP})
    assert result.is_error and body["code"] == "port_not_locked" and "set_port_lock" in body["next_step"]
    wire.routes[("DELETE", f"{KEYS_PATH}/12")] = (404, {"detail": {"title": "Not Found",
                                                                   "message": "Access key not found."}})
    result, body = await _call(mcp_client, "revoke_access_key", {"pod": WIRE_POD, "workspace": WS, "port": 8188,
                                                                 "key_id": 12, "confirm": True, "operation_id": OP})
    assert result.is_error and body["code"] == "access_key_not_found"


# --- A template whose author set a port open ---------------------------------------------------------------------

# The template, with port 8188 set open by its author; 8080 has no setting and 9100 is internal.
TEMPLATE_OPENS_8188 = {**TEMPLATE, "endpoints": [{**e, "isLocked": False} if e["containerPort"] == 8188 else e
                                                 for e in TEMPLATE["endpoints"]]}


async def test_wire_a_port_the_template_sets_open_is_created_open_and_the_user_is_asked(mcp_client, wire):
    """The SDK sends a template port the lock its author chose. A plain create therefore sends 8188 open: the preview
    says so, says why, and says how to lock it."""
    wire.routes[("GET", "/v1/sdk/templates/457")] = (200, TEMPLATE_OPENS_8188)
    result, body = await _call(mcp_client, "create_pod", WIRE_CREATE)
    assert not result.is_error, result.content
    assert _locks(wire.body("POST", "/v1/sdk/pods/estimate")) == {8188: False, 8080: True}
    assert body["open_ports"] == [8188] and body["open_by_template"] == [8188] and body["locked_ports"] == [8080]
    question = body["question"]
    assert "port 8188 will be OPEN (unlocked) because the template sets it open, not because the user asked" in question
    assert 'To lock it, call again with `template_ports` [{"port": 8188, "locked": true}] (a new preview)' in question
    assert "Tell the user and ask which they want." in question
    assert "Go ahead only if the user explicitly asked for an open port." not in question
    assert not wire.sent("POST", "/v1/sdk/pods")

    # The argument the preview names does lock it, on the wire.
    locked = _payload(await mcp_client.call_tool("create_pod", {
        **WIRE_CREATE, "template_ports": [{"port": 8188, "locked": True}]}))
    assert _locks(wire.body("POST", "/v1/sdk/pods/estimate")) == {8188: True, 8080: True}
    assert locked["locked_ports"] == [8080, 8188] and locked["open_ports"] == [] and "open_by_template" not in locked
    assert "OPEN" not in locked["question"]

    # Created as it is, the result still says that the open port is the template's doing.
    done = _payload(await mcp_client.call_tool("create_pod", {**WIRE_CREATE, "confirm": True, "operation_id": OP}))
    assert _locks(wire.body("POST", "/v1/sdk/pods")) == {8188: False, 8080: True}
    assert done["open_by_template"] == [8188] and "Port 8188 is open, as the template sets it" in done["next_step"]


async def test_wire_an_open_port_the_user_asked_for_is_not_put_down_to_the_template(mcp_client, wire):
    body = _payload(await mcp_client.call_tool("create_pod", {
        **WIRE_CREATE, "template_ports": [{"port": 8188, "locked": False}]}))
    assert _locks(wire.body("POST", "/v1/sdk/pods/estimate")) == {8188: False, 8080: True}
    assert body["open_ports"] == [8188] and "open_by_template" not in body
    assert "WARNING: port 8188 will be OPEN (unlocked): " in body["question"]
    assert "because the template sets" not in body["question"]


# --- An edit that only locks a port the pod already exposes ------------------------------------------------------

async def test_wire_locking_a_public_port_through_an_edit_while_locks_are_off(mcp_client, wire):
    wire.routes[("POST", f"{POD_PATH}/estimate")] = (409, LOCK_DISABLED)
    result, body = await _call(mcp_client, "update_pod", {"pod": WIRE_POD, "workspace": WS,
                                                          "template_ports": [{"port": 8080, "locked": True}]})
    assert _locks(wire.body("POST", f"{POD_PATH}/estimate")) == {8080: True}
    assert result.is_error and body["code"] == "endpoint_lock_unavailable" and body["reason"] == "disabled"
    assert body["next_step"].startswith("Nothing was changed: every port keeps the lock it has now.")
    assert "try again later" in body["next_step"]
    assert "lock_public_ports" not in body["next_step"] and "internal" not in body["next_step"]
    assert len(wire.sent("POST", f"{POD_PATH}/estimate")) == 1 and not wire.sent("PATCH", POD_PATH)


# --- Rolling a key and listing keys ------------------------------------------------------------------------------

ROLL_PATH = f"{KEYS_PATH}/12/roll"
ROLLED_ITEM = {**access_key_item(31, "batch-script", previous_until="2026-11-16T10:00:00Z"),
               "issuedAt": "2026-11-16T09:00:00Z", "accessKey": ROLLED_ACCESS_KEY}
WIRE_ROLL = {"pod": WIRE_POD, "workspace": WS, "port": 8188, "key_id": 12}


async def test_wire_a_roll_is_previewed_from_the_key_list_then_sent_once(mcp_client, wire):
    wire.routes[("POST", ROLL_PATH)] = (201, ROLLED_ITEM)
    preview = _payload(await mcp_client.call_tool("roll_access_key", {**WIRE_ROLL, "overlap_seconds": 86400}))
    assert preview["confirmed"] is False and preview["key"]["id"] == 12 and not wire.sent("POST", ROLL_PATH)
    assert len(wire.sent("GET", KEYS_PATH)) == 1 and NO_SECOND_CALL in preview["next_step"]

    result, body = await _call(mcp_client, "roll_access_key", {**WIRE_ROLL, "overlap_seconds": 86400,
                                                               "confirm": True,
                                                               "operation_id": preview["operation_id"]})
    assert not result.is_error, result.content
    request, = wire.sent("POST", ROLL_PATH)
    assert json.loads(request.content) == {"overlapSeconds": 86400}
    assert request.url.params["workspace"] == WS
    assert request.headers["Idempotency-Key"] != preview["operation_id"]    # not sent as a recorded write
    assert body["access_key"] == ROLLED_ACCESS_KEY and result.content[0].text.count(ROLLED_ACCESS_KEY) == 1
    assert json.dumps(result.structured_content).count(ROLLED_ACCESS_KEY) == 1
    assert ROLLED_ACCESS_KEY not in json.dumps(body["key"]) and body["key"]["id"] == 31
    assert "The old value works until 2026-11-16T10:00:00+00:00" in body["next_step"]
    assert "operation_lookup" not in body


@pytest.mark.parametrize("answer", [httpx.ReadTimeout("timed out"), (500, {"detail": "boom"})],
                         ids=["timeout", "500"])
async def test_wire_a_roll_without_a_clear_answer_is_sent_once(mcp_client, wire, answer):
    wire.routes[("POST", ROLL_PATH)] = answer
    result, body = await _call(mcp_client, "roll_access_key", {**WIRE_ROLL, "confirm": True, "operation_id": OP})
    assert result.is_error and body["code"] == "access_key_outcome_unknown"
    assert len(wire.sent("POST", ROLL_PATH)) == 1            # the SDK didn't resend it, and neither did the tool
    assert "If key 12 is still listed, nothing changed" in body["next_step"]
    assert "mshv_" not in result.content[0].text


async def test_wire_a_roll_of_a_key_that_is_gone(mcp_client, wire):
    wire.routes[("POST", ROLL_PATH)] = (404, {"detail": {"title": "Not Found", "message": "Access key not found."}})
    result, body = await _call(mcp_client, "roll_access_key", {**WIRE_ROLL, "confirm": True, "operation_id": OP})
    assert result.is_error and body["code"] == "access_key_not_found" and "access_keys" in body["next_step"]


async def test_wire_keys_are_listed_port_by_port_and_never_with_a_value(mcp_client, wire):
    """Without `port`: one GET per exposed port, and for the internal port only because it still holds a key."""
    pod = {**WIRE_POD_BODY, "template": {"endpoints": WENT_INTERNAL}}
    wire.routes[("GET", POD_PATH)] = (200, pod)
    # The API never lists a value; should a field with one slip into an item, it still doesn't reach the agent.
    wire.routes[("GET", KEYS_PATH)] = (200, {"keys": [{**access_key_item(12, "batch-script"),
                                                       "accessKey": ACCESS_KEY}]})
    wire.routes[("GET", f"{POD_PATH}/ports/8080/keys")] = (200, {"keys": []})
    wire.routes[("GET", f"{POD_PATH}/ports/9100/keys")] = (200, {"keys": [access_key_item(40, "old", port=9100)]})
    result, body = await _call(mcp_client, "access_keys", {"pod": WIRE_POD, "workspace": WS})
    assert not result.is_error, result.content
    listed = [r.url.path for r in wire.requests if r.method == "GET" and r.url.path.endswith("/keys")]
    assert listed == [KEYS_PATH, f"{POD_PATH}/ports/8080/keys", f"{POD_PATH}/ports/9100/keys"]
    assert all(r.url.params["workspace"] == WS for r in wire.requests if r.url.path.endswith("/keys"))
    assert [(k["id"], k["port"]) for k in body["items"]] == [(12, 8188), (40, 9100)]
    assert [p["access"] for p in body["ports"]] == ["locked", "unlocked", "internal"]
    assert "Port 9100 is internal now" in body["next_step"]
    assert "mshv_Zq7Zq7Zq7" not in result.content[0].text and "access_key\"" not in result.content[0].text

    one = _payload(await mcp_client.call_tool("access_keys", {"pod": WIRE_POD, "workspace": WS, "port": 8080}))
    assert one["items"] == [] and one["ports"] == [{"port": 8080, "name": "ide", "access": "unlocked"}]


# =====================================================================================================================
# A lock choice that is not in effect yet
# =====================================================================================================================

PENDING_ENDPOINTS = [{**LOCK_ENDPOINTS[0], "lockState": "locking"},
                     {**LOCK_ENDPOINTS[1], "lockState": "unlocking"}, LOCK_ENDPOINTS[2]]


async def test_pods_show_each_endpoints_lock_state(mcp_client, fake, with_key):
    _setup(fake, endpoints=PENDING_ENDPOINTS)
    body = _payload(await mcp_client.call_tool("pods", {"pod": POD}))
    assert [(e["port"], e["access"], e["lock_state"]) for e in body["pod"]["endpoints"]] == [
        (8188, "locked", "locking"), (8080, "unlocked", "unlocking"), (9100, "internal", None)]


async def test_pods_from_a_server_without_lock_states_say_null(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    body = _payload(await mcp_client.call_tool("pods", {"pod": POD}))
    assert [e["lock_state"] for e in body["pod"]["endpoints"]] == [None, None, None]


async def test_locking_a_port_says_when_it_is_not_in_effect_yet(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS, toggle_state="locking")
    result, body = await _call(mcp_client, "set_port_lock", {"pod": POD, "port": 8080, "locked": True,
                                                             "operation_id": OP})
    assert not result.is_error and body["state"] == "locking" and body["locked"] is True
    assert ("takes a few seconds to take effect, and until `lock_state` is `locked` (read it with the pods tool) "
            "the port can still be reached without a key") in body["next_step"]
    assert "Then, only workspace members signed in" in body["next_step"]
    assert "any other request gets 401, which is the lock working, not a fault" in body["next_step"]


async def test_locking_that_is_in_effect_already_says_so(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS, toggle_state="locked")
    body = _payload(await mcp_client.call_tool("set_port_lock", {"pod": POD, "port": 8080, "locked": True,
                                                                 "operation_id": OP}))
    assert body["state"] == "locked"
    assert body["next_step"].startswith("Port 8080 is locked; it applies without a restart. Only workspace members")
    assert "can still be reached" not in body["next_step"]


async def test_unlocking_a_port_says_when_it_is_not_in_effect_yet(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS, toggle_state="unlocking")
    done = _payload(await mcp_client.call_tool("set_port_lock", {"pod": POD, "port": 8188, "locked": False,
                                                                 "confirm": True, "operation_id": OP}))
    assert done["state"] == "unlocking" and done["locked"] is False
    assert ("Port 8188 is being unlocked: until `lock_state` is `unlocked` (read it with the pods tool) requests may "
            f"still get 401. Then {OPEN_TO_ANYONE}.") in done["next_step"]


async def test_a_lock_answer_from_a_server_without_states_has_no_state(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    body = _payload(await mcp_client.call_tool("set_port_lock", {"pod": POD, "port": 8080, "locked": True,
                                                                 "operation_id": OP}))
    assert "state" not in body and "Port 8080 is locked; it applies without a restart." in body["next_step"]


async def test_unlocking_a_port_that_is_being_unlocked_says_it_is_not_in_effect_yet(mcp_client, fake, with_key):
    _setup(fake, endpoints=[LOCK_ENDPOINTS[0], {**LOCK_ENDPOINTS[1], "lockState": "unlocking"}])
    body = _payload(await mcp_client.call_tool("set_port_lock", {"pod": POD, "port": 8080, "locked": False}))
    assert "unlocked already; nothing was changed. It is not in effect yet (`lock_state` unlocking)." in body["next_step"]


async def test_the_descriptions_and_instructions_explain_the_lock_state(mcp_client):
    from meshive_mcp.server import INSTRUCTIONS

    tools = {t.name: t.description for t in (await mcp_client.list_tools()).tools}
    assert "`lock_state` is locking or unlocking until then" in tools["pods"]
    assert "Repeating the call doesn't speed this up" in tools["set_port_lock"]
    assert "the answer's `state` is `locking` or `unlocking`" in tools["set_port_lock"]
    assert "`lock_state` is `locking` or `unlocking` until it is in effect" in INSTRUCTIONS


# =====================================================================================================================
# Expired access keys
# =====================================================================================================================

EXPIRED_KEY = {**access_key_item(14, "old-ci"), "expiresAt": "2026-11-10T09:00:00Z", "expired": True}


async def test_access_keys_show_which_keys_expired(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS, access_keys={8188: [access_key_item(12, "batch-script"), EXPIRED_KEY]})
    result, body = await _call(mcp_client, "access_keys", {"pod": POD, "port": 8188})
    assert not result.is_error
    assert [(k["id"], k["expired"]) for k in body["items"]] == [(12, False), (14, True)]
    desc = {t.name: t.description for t in (await mcp_client.list_tools()).tools}
    assert "stays listed for 30 days" in desc["access_keys"] and "roll_access_key" in desc["access_keys"]
    assert "replace a key that has expired" in desc["roll_access_key"]


async def test_rolling_an_expired_key_is_previewed_without_an_overlap(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS, access_keys={8188: [EXPIRED_KEY]})
    result, body = await _call(mcp_client, "roll_access_key", {**ROLL_ARGS, "key_id": 14})
    assert not result.is_error and body["confirmed"] is False and body["key"]["expired"] is True
    question = body["question"]
    assert question.startswith(f"Roll expired access key 'old-ci' (mshv_Zq7Zq7Zq…) of port 8188 of pod '{POD}'?")
    assert "no overlap" in question and "stops working" not in question
    assert "overlap_seconds" not in body
    assert not _calls(fake, "roll_access_key")
    done = _payload(await mcp_client.call_tool("roll_access_key", {**ROLL_ARGS, "key_id": 14, "overlap_seconds": 0,
                                                                   "confirm": True, "operation_id": OP}))
    assert done["access_key"] == ROLLED_ACCESS_KEY


async def test_a_key_that_is_gone_says_expired_keys_stay_for_30_days(mcp_client, fake, with_key):
    _setup(fake, endpoints=LOCK_ENDPOINTS)
    result, body = await _call(mcp_client, "roll_access_key", {**ROLL_ARGS, "key_id": 99})
    assert result.is_error and body["code"] == "access_key_not_found"
    assert "has no access key with id 99" in body["message"]
    assert "expired more than 30 days ago" in body["next_step"]


async def test_wire_expired_keys_are_listed_and_rolled(mcp_client, wire):
    wire.routes[("GET", KEYS_PATH)] = (200, {"keys": [EXPIRED_KEY]})
    wire.routes[("POST", f"{KEYS_PATH}/14/roll")] = (201, {**ROLLED_ITEM, "previousValueValidUntil": None})
    body = _payload(await mcp_client.call_tool("access_keys", {"pod": WIRE_POD, "workspace": WS, "port": 8188}))
    assert [(k["id"], k["expired"]) for k in body["items"]] == [(14, True)]
    preview = _payload(await mcp_client.call_tool("roll_access_key", {**WIRE_ROLL, "key_id": 14}))
    assert preview["question"].startswith("Roll expired access key")
    done = _payload(await mcp_client.call_tool("roll_access_key", {**WIRE_ROLL, "key_id": 14, "overlap_seconds": 0,
                                                                   "confirm": True,
                                                                   "operation_id": preview["operation_id"]}))
    assert wire.body("POST", f"{KEYS_PATH}/14/roll") == {"overlapSeconds": 0}
    assert done["key"]["previous_value_valid_until"] is None and "The old value no longer works" in done["next_step"]
