"""HTTP transport: /healthz, Bearer header passthrough, GET /mcp rejection."""
import json
import logging
import socket
import sys
import threading
import time

import anyio
import httpx
import pytest
import uvicorn
from mcp.server.mcpserver import server as sdk_server
from starlette.responses import PlainTextResponse

pytestmark = pytest.mark.anyio

from meshive_mcp.__main__ import BoundedHeadProtocol, BoundedLines, configure_logging, main, raise_open_file_limit
from meshive_mcp.server import (AdmitRequests, OmitContentLength, RefuseStreams, _may_be_large, _read_and_drop,
                                _too_complex, create_http_app, create_server)

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(method, params=None, id=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id is not None:
        body["id"] = id
    return body


@pytest.fixture
async def http():
    app = create_http_app(create_server())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as c:
        # Run the lifespan directly — ASGITransport doesn't send the startup event.
        async with app.router.lifespan_context(app):
            yield c


async def test_healthz(http, monkeypatch):
    """Deployment-verification contract: the MCP/SDK revisions baked into the image and the installed SDK version show as-is. null in local builds."""
    import meshive
    monkeypatch.setenv("MESHIVE_MCP_REVISION", "abc123")
    monkeypatch.setenv("MESHIVE_SDK_REVISION", "def456")
    body = (await http.get("/healthz")).json()
    assert body["status"] == "ok" and body["revision"] == "abc123" and body["sdk_revision"] == "def456"
    assert body["sdk_version"] == meshive.__version__
    monkeypatch.delenv("MESHIVE_MCP_REVISION")
    assert (await http.get("/healthz")).json()["revision"] is None


async def test_get_mcp_is_not_a_stream(http):
    r = await http.get("/mcp", headers={"Accept": "text/event-stream"})
    assert r.status_code == 405


@pytest.mark.parametrize("method, path, headers, body", [
    ("GET", "/healthz", {}, None),
    ("GET", "/healthz", {"MCP-Protocol-Version": "2025-06-18"}, None),
    ("GET", "/mcp", {}, None),
    ("GET", "/.well-known/oauth-protected-resource/mcp", {}, None),
    ("POST", "/mcp", HEADERS, _rpc("ping")),
    ("POST", "/mcp", HEADERS, _rpc("notifications/initialized", id=None)),
])
async def test_responses_carry_no_content_length(http, method, path, headers, body):
    """Norton breaks MCP responses that have a Content-Length, even short and empty ones."""
    r = await http.request(method, path, headers=headers, json=body)
    assert "content-length" not in r.headers


async def test_body_over_the_limit_is_refused(http):
    """Deployments don't buffer requests at the proxy. The size is written out so that raising the limit fails here."""
    limit = 1024 * 1024

    async def in_pieces():
        for _ in range(2):
            yield b"x" * limit

    assert (await http.post("/mcp", headers=HEADERS, content=b"x" * limit)).status_code == 400  # read in full, not JSON
    over = await http.post("/mcp", headers=HEADERS, content=b"x" * (limit + 1))
    assert over.status_code == 413 and "content-length" not in over.headers
    assert (await http.post("/mcp", headers=HEADERS, content=in_pieces())).status_code == 413


async def test_large_task_script_fits_the_limit(http):
    """JSON escaping doubles a script of quotes, so 256 KiB of them is a body of about 512 KiB."""
    arguments = {"name": "t", "script": '"' * (256 * 1024)}
    r = await http.post("/mcp", headers=HEADERS, json=_rpc("tools/call", {"name": "estimate_task", "arguments": arguments}))
    assert r.status_code == 200
    assert json.loads(r.json()["result"]["content"][0]["text"])["code"] == "no_api_key"


async def test_body_within_the_limit_reaches_the_server(http):
    async def in_pieces():
        raw = json.dumps(_rpc("ping")).encode()
        yield raw[:10]
        yield raw[10:]

    r = await http.post("/mcp", headers=HEADERS, content=in_pieces())
    assert r.status_code == 200 and r.json()["id"] == 1


MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test-agent", "version": "9.9"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


LARGE = 64 * 1024  # Written out, like the body limit above, so that changing it fails here.


def _string(size):
    """A JSON string of exactly this many bytes."""
    return b'"' + b"x" * (size - 2) + b'"'


def _held_app(hold_over):
    """A handler that holds bodies over a size until released, behind the real admission check."""
    release, held = anyio.Event(), []

    async def handler(scope, receive, send):
        if scope["path"] == "/mcp" and len((await receive())["body"]) > hold_over:
            held.append(scope)
            await release.wait()
        await PlainTextResponse("done")(scope, receive, send)

    return AdmitRequests(handler), release, held


async def _with_full_slots(hold_over, count, size, check):
    app, release, held = _held_app(hold_over)
    statuses = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as c:
        async def post():
            statuses.append((await c.post("/mcp", content=_string(size))).status_code)

        with anyio.fail_after(30):
            async with anyio.create_task_group() as tg:
                for _ in range(count):
                    tg.start_soon(post)
                while len(held) < count:
                    await anyio.sleep(0.01)
                await check(c)
                release.set()
            await post()  # The slots came back.
    assert statuses == [200] * (count + 1)


async def _assert_busy(c, size):
    refused = await c.post("/mcp", content=_string(size))
    assert refused.status_code == 503 and refused.headers["retry-after"] == "1"
    assert refused.json()["error"]["message"].startswith("Server busy")


async def test_large_bodies_are_refused_while_three_are_being_handled():
    async def check(c):
        await _assert_busy(c, LARGE + 1)
        assert (await c.post("/mcp", content=_string(LARGE))).status_code == 200  # Smaller bodies still pass.
        assert (await c.get("/healthz")).status_code == 200

    await _with_full_slots(LARGE, 3, LARGE + 1, check)


async def test_requests_are_refused_while_32_are_being_handled():
    async def check(c):
        await _assert_busy(c, 3)
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.post("/healthz", content=b"x")).status_code == 200

    await _with_full_slots(0, 32, 3, check)


def _post_scope(length=None):
    headers = [] if length is None else [(b"content-length", str(length).encode())]
    return {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}


@pytest.mark.parametrize("length, large", [(LARGE, False), (LARGE + 1, True), (None, True)])
def test_a_body_of_unstated_size_counts_as_large(length, large):
    assert _may_be_large(_post_scope(length)) is large


def test_a_stated_size_counts_for_nothing_on_a_chunked_body():
    scope = _post_scope(1)
    scope["headers"].append((b"transfer-encoding", b"chunked"))
    assert _may_be_large(scope)


async def test_large_uploads_count_from_when_they_arrive():
    """Reading a body takes memory too, so one that is still arriving holds its slot."""
    called, sent = [], []
    arrived = anyio.Event()

    async def handler(scope, receive, send):
        called.append(scope)

    async def stalled():
        await arrived.wait()
        return {"type": "http.disconnect"}

    async def one_message():
        return {"type": "http.request", "body": b'"x"', "more_body": False}

    async def send(message):
        sent.append(message)

    gate = AdmitRequests(handler)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            for _ in range(3):
                tg.start_soon(gate, _post_scope(LARGE + 1), stalled, None)
            while gate.active_large < 3:
                await anyio.sleep(0.01)
            await gate(_post_scope(LARGE + 1), one_message, send)
            assert sent[0]["status"] == 503 and called == []
            arrived.set()
    assert gate.active_large == 0


async def test_a_refused_body_is_read_to_its_end_so_that_the_client_can_read_the_refusal():
    messages = [{"type": "http.request", "body": b"x" * 1024, "more_body": True} for _ in range(10)]
    messages.append({"type": "http.request", "body": b"", "more_body": False})
    sent = []

    async def handler(scope, receive, send):
        raise AssertionError("A refused body reached the SDK.")

    async def receive():
        return messages.pop(0)

    async def send(message):
        assert messages == []  # A client that is still writing may never read what comes back.
        sent.append(message)

    gate = AdmitRequests(handler)
    gate.active_large = 3
    await gate(_post_scope(LARGE + 1), receive, send)
    assert sent[0]["status"] == 503


async def test_reading_a_refused_body_stops_at_the_body_limit():
    messages = [{"type": "http.request", "body": b"x" * (512 * 1024), "more_body": True} for _ in range(3)]

    async def receive():
        return messages.pop(0)

    await _read_and_drop(receive)
    assert len(messages) == 1


async def test_admission_frees_its_slots_when_the_handler_fails():
    async def failing(scope, receive, send):
        await receive()
        raise RuntimeError("boom")

    gate = AdmitRequests(failing)

    async def receive():
        return {"type": "http.request", "body": _string(LARGE + 1), "more_body": False}

    with pytest.raises(RuntimeError):
        await gate(_post_scope(LARGE + 1), receive, None)
    assert gate.active == 0 and gate.active_large == 0


@pytest.mark.parametrize("messages", [
    [{"type": "http.disconnect"}],
    [{"type": "http.request", "body": b"x", "more_body": True}, {"type": "http.disconnect"}],
])
async def test_body_from_a_client_that_left_is_not_handled(messages):
    called, sent = [], []

    async def handler(scope, receive, send):
        called.append(scope)

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    gate = AdmitRequests(handler)
    await gate(_post_scope(), receive, send)
    assert called == [] and sent == [] and gate.active_large == 0


def test_admission_sits_inside_the_framing():
    """Refusals are chunked like every response."""
    stack = [m.cls for m in create_http_app(create_server()).user_middleware]
    assert stack == [OmitContentLength, AdmitRequests, RefuseStreams]


def _list(commas):
    return b"[" + b"0," * commas + b"0]"


@pytest.mark.parametrize("body, too_complex", [
    (b"[" * 32 + b"]" * 32, False),
    (b"[" * 33 + b"]" * 33, True),
    (b'{"a":' * 32 + b"0" + b"}" * 32, False),
    (b'{"a":' * 33 + b"0" + b"}" * 33, True),
    (_list(8190), False),
    (_list(8191), True),
    (b'"' * (4 * 8192), False),  # Stops early past this: splitting on quotes is what costs memory.
    (b'"' * (4 * 8192 + 1), True),
    (b'"' + b"[" * 1000 + b'"', False),  # All inside a string.
    (b'"' + b'\\"[' * 1000 + b'"', False),  # An escaped quote doesn't end the string.
    (b'"\\\\"' + b"[" * 33, True),  # An escaped backslash doesn't escape the quote after it.
    (b"[" + b'"a":"b",' * 8191 + b"0]", True),
    (b"x" * LARGE, False),
])
def test_structure_is_measured_outside_strings(body, too_complex):
    assert _too_complex(body) is too_complex


async def test_too_complex_body_is_refused_before_the_sdk_parses_it(http):
    r = await http.post("/mcp", headers=HEADERS, content=b"[" * 33 + b"]" * 33)
    assert r.status_code == 400 and "content-length" not in r.headers
    assert r.json()["error"]["message"] == "The request has too many values or is nested too deeply."


async def test_brackets_in_a_task_script_are_not_structure(http):
    arguments = {"name": "t", "script": "[" * (256 * 1024)}
    r = await http.post("/mcp", headers=HEADERS, json=_rpc("tools/call", {"name": "estimate_task", "arguments": arguments}))
    assert json.loads(r.json()["result"]["content"][0]["text"])["code"] == "no_api_key"


MODERN_LIST = {**HEADERS, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"}


@pytest.mark.parametrize("encode", [
    lambda text: text.encode("utf-16-le"),
    lambda text: text.encode("utf-16-be"),
    lambda text: text.encode("utf-16"),
    lambda text: text.encode("utf-32"),
    lambda text: text.encode("utf-8-sig"),
])
async def test_only_utf8_reaches_the_sdk(http, encode):
    """The SDK's newest protocol path decodes these too."""
    body = json.dumps(_rpc("tools/list", {"_meta": MODERN_META}))
    r = await http.post("/mcp", headers=MODERN_LIST, content=encode(body))
    assert r.status_code == 400 and r.json()["error"]["message"] == "The request isn't UTF-8 JSON."


async def test_utf16_cant_hide_structure_from_the_sdk(http):
    """In UTF-16 an escaped quote looks like a real one to the byte scan, so what follows it looks like a string."""
    deep = json.loads("[" * 33 + "]" * 33)
    body = json.dumps(_rpc("tools/list", {"_meta": {**MODERN_META, "x": '"', "y": deep}}))
    assert not _too_complex(body.encode("utf-16-le"))
    r = await http.post("/mcp", headers=MODERN_LIST, content=body.encode("utf-16-le"))
    assert r.status_code == 400 and r.json()["error"]["message"] == "The request isn't UTF-8 JSON."


@pytest.mark.parametrize("key", ["inputResponses", "inputRespons\\u0065s"])
async def test_input_responses_are_refused(http, key):
    body = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"account","arguments":{},"%s":{"a":{}}}}' % key
    r = await http.post("/mcp", headers=HEADERS, content=body)
    assert r.status_code == 400 and "inputResponses" in r.json()["error"]["message"]


def _download_links(paths):
    return _rpc("tools/call", {"name": "download_links", "arguments": {"paths": paths}})


def _tool_error(response):
    return json.loads(response.json()["result"]["content"][0]["text"])


@pytest.mark.parametrize("paths", [[0] * 4096, json.dumps([0] * 4096)])  # The list itself is a value too.
async def test_too_many_argument_values_are_refused_before_validation(http, paths):
    """Validation reports every bad item, so this would otherwise answer with one error per item."""
    r = await http.post("/mcp", headers=HEADERS, json=_download_links(paths))
    assert r.status_code == 200 and r.json()["result"]["isError"] and len(r.content) < 1024
    assert _tool_error(r)["code"] == "invalid_argument"


async def test_nested_argument_values_count_towards_the_limit(http):
    nested = {"env": {"KEY": [[{"a": 0}] * 1100] * 2}}
    r = await http.post("/mcp", headers=HEADERS, json=_rpc("tools/call", {"name": "estimate_task", "arguments": nested}))
    assert len(r.content) < 1024 and _tool_error(r)["code"] == "invalid_argument"


@pytest.mark.parametrize("paths", [["*"] * 4095, json.dumps(["*"] * 4095)])
async def test_arguments_at_the_limit_reach_the_tool(http, paths):
    text = _tool_error(await http.post("/mcp", headers=HEADERS, json=_download_links(paths)))
    assert text["code"] == "invalid_argument" and "exactly one" in text["message"]  # The tool's own check, not the limit.


async def test_unknown_tool_gets_the_ordinary_answer(http):
    r = await http.post("/mcp", headers=HEADERS, json=_rpc("tools/call", {"name": "nope", "arguments": {}}))
    payload = r.json()["result"]
    assert payload["isError"] and "Unknown tool" in payload["content"][0]["text"]


def test_refused_calls_are_not_logged_at_the_default_level():
    """The SDK logs the caller's tool name and error text at INFO, which can be as long as the request."""
    logger = logging.getLogger(sdk_server.__name__)
    before = logger.level
    try:
        configure_logging("INFO")
        assert logger.level == logging.WARNING
        logger.setLevel(logging.NOTSET)
        configure_logging("DEBUG")
        assert logger.level == logging.NOTSET
    finally:
        logger.setLevel(before)


def test_log_lines_keep_their_two_ends_within_the_limit():
    try:
        raise ValueError("x" * 2_000_000 + "end")
    except ValueError:
        record = logging.LogRecord("n", logging.ERROR, "f", 1, "start", None, sys.exc_info())
    line = BoundedLines("%(message)s").format(record)
    assert len(line) <= 8192 + 5 and line.startswith("start") and line.endswith("end")


def test_log_lines_are_bounded_on_the_handler_the_server_ends_up_with(monkeypatch):
    """The SDK sets up logging too, and logging.basicConfig does nothing once a handler exists."""
    runs = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: runs.append(kwargs))
    monkeypatch.setattr("meshive_mcp.__main__.raise_open_file_limit", lambda: None)
    root = logging.getLogger()
    logger = logging.getLogger(sdk_server.__name__)
    handlers, level = root.handlers[:], logger.level
    root.handlers[:] = []
    try:
        main([])
        assert isinstance(root.handlers[0].formatter, BoundedLines)
    finally:
        root.handlers[:] = handlers
        logger.setLevel(level)
    assert runs[0]["http"] is BoundedHeadProtocol


@pytest.fixture
def sdk_log_level():
    """main() quiets the SDK's logger for the rest of the process."""
    logger = logging.getLogger(sdk_server.__name__)
    level = logger.level
    yield
    logger.setLevel(level)


def test_http_server_raises_its_open_file_limit_to_the_hard_one(monkeypatch, sdk_log_level):
    resource = pytest.importorskip("resource")
    order = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (1024, 524288))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: order.append(("setrlimit", which, limits)))
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: order.append(("run",)))
    main([])
    assert order == [("setrlimit", resource.RLIMIT_NOFILE, (524288, 524288)), ("run",)]


def test_http_server_starts_when_the_open_file_limit_cant_be_raised(monkeypatch, caplog, sdk_log_level):
    resource = pytest.importorskip("resource")
    runs = []

    def refuse(which, limits):
        raise ValueError("not allowed")

    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 524288))
    monkeypatch.setattr(resource, "setrlimit", refuse)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: runs.append(kwargs))
    with caplog.at_level(logging.WARNING):
        main([])
    assert runs and "Couldn't raise the open file limit from 256." in caplog.text


def test_open_file_limit_with_an_unlimited_hard_one_is_raised_to_open_max(monkeypatch):
    resource = pytest.importorskip("resource")
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: calls.append(limits))
    raise_open_file_limit()
    assert calls == [(10240, resource.RLIM_INFINITY)]


def test_open_file_limit_already_as_high_is_left_alone(monkeypatch):
    resource = pytest.importorskip("resource")
    monkeypatch.setattr(resource, "setrlimit", lambda which, limits: pytest.fail("setrlimit called"))
    for limits in ((4096, 4096), (resource.RLIM_INFINITY, resource.RLIM_INFINITY), (20000, resource.RLIM_INFINITY)):
        monkeypatch.setattr(resource, "getrlimit", lambda which, limits=limits: limits)
        raise_open_file_limit()


@pytest.fixture
def server_port():
    """A real uvicorn server with the protocol main() uses, on a free port."""
    async def app(scope, receive, send):
        await PlainTextResponse("ok")(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, http=BoundedHeadProtocol, lifespan="off",
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(500):
        if server.started:
            break
        time.sleep(0.01)
    yield server.servers[0].sockets[0].getsockname()[1]
    server.should_exit = True
    thread.join(10)


def _status(port, path="/", fields=0):
    head = f"GET {path} HTTP/1.1\r\nHost: t\r\n" + "x-aaaa: 1\r\n" * fields + "Connection: close\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(head.encode())
        return s.recv(64).split(b" ")[1]


@pytest.mark.parametrize("path, fields, status", [
    ("/", 1400, b"200"),  # 11 bytes counted for each field.
    ("/", 1600, b"400"),
    ("/" + "a" * 15000, 0, b"200"),
    ("/" + "a" * 17000, 0, b"400"),
])
def test_request_heads_are_limited_as_h11_did(server_port, path, fields, status):
    """Every header field becomes a Python object, so a head of many fields costs far more memory than its size."""
    assert _status(server_port, path, fields) == status
    assert _status(server_port) == b"200"


def test_the_head_limit_is_per_request_on_a_kept_alive_connection(server_port):
    head = ("GET / HTTP/1.1\r\nHost: t\r\n" + "x-aaaa: 1\r\n" * 1400 + "\r\n").encode()
    with socket.create_connection(("127.0.0.1", server_port), timeout=10) as s:
        for _ in range(2):
            s.sendall(head)
            answer = b""
            while not answer.endswith(b"ok"):
                chunk = s.recv(4096)
                assert chunk, answer
                answer += chunk
            assert answer.split(b" ")[1] == b"200"


async def _modern_post(http, method, **params):
    headers = {**HEADERS, "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    with anyio.fail_after(5):
        return await http.post("/mcp", headers=headers, json=_rpc(method, {"_meta": MODERN_META, **params}))


async def test_discover_offers_no_change_notifications(http):
    """A client that sees listChanged opens subscriptions/listen, and waits on it if a proxy buffers the stream."""
    r = await _modern_post(http, "server/discover")
    assert r.status_code == 200, r.text
    capabilities = r.json()["result"]["capabilities"]
    assert capabilities["tools"]["listChanged"] is False
    assert capabilities["prompts"]["listChanged"] is False
    assert capabilities["resources"]["listChanged"] is False


async def test_listen_is_refused_instead_of_held_open(http):
    r = await _modern_post(http, "subscriptions/listen", notifications={"toolsListChanged": True})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == -32601


async def test_bearer_reaches_sdk_client(http, fake):
    key = "meshive_" + "b" * 64
    init = await http.post("/mcp", headers={**HEADERS, "Authorization": f"Bearer {key}"},
                           json=_rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                    "clientInfo": {"name": "test-agent", "version": "9.9"}}))
    assert init.status_code == 200, init.text
    r = await http.post("/mcp", headers={**HEADERS, "Authorization": f"Bearer {key}", "User-Agent": "codex/1.0"},
                        json=_rpc("tools/call", {"name": "account", "arguments": {}}, id=2))
    assert r.status_code == 200, r.text
    payload = r.json()["result"]
    assert not payload.get("isError"), payload
    body = json.loads(payload["content"][0]["text"])
    assert body["user"]["email"] == "u@example.com"
    assert fake["last"].key == key
    # Stateless, so this request has no clientInfo → User-Agent fallback
    assert fake["last"].label in ("test-agent/9.9", "codex/1.0")


async def test_missing_bearer_is_tool_error_not_http_401(http, fake):
    r = await http.post("/mcp", headers=HEADERS, json=_rpc("tools/call", {"name": "account", "arguments": {}}))
    assert r.status_code == 200
    payload = r.json()["result"]
    assert payload["isError"] and json.loads(payload["content"][0]["text"])["code"] == "no_api_key"
