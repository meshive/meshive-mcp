"""Every published tool carries a scope; sensitive SDK responses terminate in the owner's browser."""
import json

import httpx
import pytest
from oauth_tool_cases import CASES, VARIANTS
from test_http import http, HEADERS, _rpc
from test_oauth_http import private
from meshive_mcp import oauth
from meshive_mcp.settings import settings

pytestmark = pytest.mark.anyio
pytest_plugins = ("oauth_case_fixtures",)


@pytest.mark.parametrize("case", (*CASES, *VARIANTS), ids=lambda c: c.tool)
async def test_published_cases_enforce_scope_before_the_sdk(private, http, case):
    required = oauth.required_scope(case.tool, case.arguments)
    assert required == ("read" if case.scope == "public" else case.scope)
    assert oauth.console_only(case.tool, case.arguments) == (case.delivery == "console")
    if case.scope == "public":
        return
    for token, status in ((None, 401), ("mshoa_reader", 403) if "write" in required else ("mshoa_expired", 401)):
        if token is None and case.tool == "gpus" and case.arguments.get("rental_type", "demand") != "task":
            continue  # This variant also has an anonymous price-only result.
        r = await http.post("/mcp", headers={**HEADERS, **({"Authorization": "Bearer " + token} if token else {})},
                            json=_rpc("tools/call", {"name": case.tool, "arguments": case.arguments}))
        assert r.status_code == status
        assert r.json()["result"]["isError"] and all(q.url.path.endswith("/inspect") for q in private)


async def test_confirmed_install_command_only_returns_console_link(private, http, monkeypatch):
    calls = []
    async def service(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/inspect"):
            return httpx.Response(200, json={"scope": "read write"})
        assert request.url.path.endswith("/actions"), request.url
        payload = json.loads(request.content)
        assert payload["tool"] == "host_install_command" and payload["operation_id"] == "confirmed-operation"
        return httpx.Response(200, json={"console_url": "https://console.example.test/oauth/action?request=test",
            "operation_id": payload["operation_id"], "status": "pending", "command": "must-never-leak"})
    monkeypatch.setattr(oauth, "private_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(service),
                                                                         base_url=settings.oauth_internal_url))
    response = await http.post("/mcp", headers={**HEADERS, "Authorization": "Bearer mshoa_writer"}, json=_rpc(
        "tools/call", {"name": "host_install_command", "arguments": {"confirm": True, "operation_id": "confirmed-operation"}}))
    assert response.status_code == 200 and "console_url" in response.text and "must-never-leak" not in response.text
    assert calls == ["/internal/oauth/inspect", "/internal/oauth/actions"]


@pytest.mark.parametrize("case", [c for c in (*CASES, *VARIANTS) if c.delivery == "console"], ids=lambda c: c.tool)
async def test_sensitive_sdk_operations_stop_before_publication(private, http, monkeypatch, case):
    prepared = []
    async def service(req):
        if req.url.path.endswith("/inspect"):
            return httpx.Response(200, json={"scope": "read write"})
        if req.url.path.endswith("/exchange"):
            return httpx.Response(200, json={"credential": "mshod_helper"})
        if req.url.path.endswith("/actions"):
            packet = json.loads(req.content)
            prepared.append(packet)
            return httpx.Response(200, json={"console_url": "https://console.example.test/oauth/action?request=test",
                "status": "pending", "operation_id": packet["operation_id"], "inferenceKey": "unexpected-value"})
        if req.url.path == "/v1/sdk/workspaces":
            return httpx.Response(200, json=[{"namespaceName": "0123456789abcdef", "workspaceName": "research",
                                              "memberRole": "admin"}])
        if req.url.path == "/v1/sdk/storages":
            return httpx.Response(200, json={"storages": [{"pvName": "pv-a", "userAlias": "test"}]})
        raise AssertionError(req.url)
    monkeypatch.setattr(oauth, "private_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(service),
                                                                         base_url=settings.oauth_internal_url))
    response = await http.post("/mcp", headers={**HEADERS, "Authorization": "Bearer mshoa_writer"}, json=_rpc(
        "tools/call", {"name": case.tool, "arguments": case.arguments}))
    assert len(prepared) == 1, response.text
    assert prepared[0]["tool"] == case.tool and response.json()["result"]["structuredContent"]["status"] == "pending"
    if case.tool in {"create_inference_key", "roll_inference_key"}:
        creating = case.tool == "create_inference_key"
        path = "/v1/sdk/inference-keys" + ("" if creating else "/3/roll")
        assert prepared[0]["method"] == "POST"
        assert prepared[0]["target"] == path + "?workspace=" + case.arguments["workspace"]
        assert prepared[0]["body"] == ({"name": "batch", "expiresInDays": 30} if creating else {})
        assert prepared[0]["operation_id"] == case.arguments["operation_id"]
    assert "unexpected-value" not in response.text


async def test_unrecognized_secret_issuing_route_cannot_exchange(private):
    async with oauth.private_http() as backend:
        transport = oauth.DelegatedTransport(backend, "mshoa_writer", "create_inference_key")
        sensitive = oauth.action_inputs.set((True, "test-operation"))
        try:
            request = httpx.Request("POST", settings.oauth_internal_url + "/v1/sdk/inference-keys/new")
            with pytest.raises(httpx.RequestError, match="Unsupported console confirmation"):
                await transport.handle_async_request(request)
        finally:
            oauth.action_inputs.reset(sensitive)
    assert not private


@pytest.mark.parametrize("case", [c for c in (*CASES, *VARIANTS) if c.sdk and c.delivery != "console"], ids=lambda c: c.tool)
async def test_authorized_oauth_call_reaches_expected_business_method(private, http, fake, case, monkeypatch, case_prerequisites):
    from conftest import FakeMeshive
    from meshive_mcp import client
    from meshive.exceptions import AuthenticationError
    calls = []
    async def observed(self, *args, **kwargs):
        calls.append(kwargs)
        raise AuthenticationError(401, "Stopped at an isolated business boundary")
    observed.__name__ = case.sdk
    monkeypatch.setattr(FakeMeshive, case.sdk, observed, raising=False)
    monkeypatch.setattr(client, "AsyncMeshive", lambda key, **kw: client._new_client(key, "test"))
    bearer = "mshoa_reader" if case.scope == "read" else "mshoa_writer"
    r = await http.post("/mcp", headers={**HEADERS, "Authorization": "Bearer " + bearer}, json=_rpc("tools/call", {
        "name": case.tool, "arguments": case.arguments}))
    assert r.status_code == 200 and len(calls) == 1, r.text
    if case.retry == "same operation ID":
        assert calls[0]["idempotency_key"] == case.arguments["operation_id"]


async def test_oauth_file_transfer(private, http, fake, monkeypatch, tmp_path):
    from meshive_mcp import client
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route
    monkeypatch.setattr(client, "AsyncMeshive", lambda key, **kw: client._new_client(key, "test"))
    async def invoke(name, args):
        response = await http.post("/mcp", headers={**HEADERS, "Authorization": "Bearer mshoa_writer"},
                                   json=_rpc("tools/call", {"name": name, "arguments": args}))
        result = response.json()["result"]
        assert not result.get("isError"), result
        return result["structuredContent"]
    source = tmp_path / "sample.txt"
    source.write_bytes(b"isolated upload test")
    payload = source.read_bytes()
    storage = {}
    async def accept(req):
        assert "authorization" not in req.headers and req.query_params["sig"] == "x"
        storage[req.path_params["fid"]] = await req.body()
        return Response(status_code=200)
    obj = Starlette(routes=[Route("/put/{fid}", accept, methods=["PUT"])])
    upload = await invoke("upload_asset", {"files": [{"path": source.name, "size_bytes": len(payload)}]})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=obj)) as uploader:
        assert (await uploader.put(upload["files"][0]["url"], content=payload)).is_success
    assert storage["1"] == source.read_bytes()
    completed = await invoke("finish_upload", {"upload_id": upload["upload_id"]})
    assert completed.get("asset", completed)["status"] == "ready"


async def test_billing_confirmation_binds_arguments_and_reads_the_sdk_operation(private, http, monkeypatch):
    from conftest import FakeMeshive
    receipts, records = {}, {}
    writes = []

    async def backend(req):
        path = req.url.path
        if path == "/internal/oauth/inspect":
            return httpx.Response(200, json={"scope": "read write"})
        if "/previews/" in path:
            data = json.loads(req.content)
            key = (data["tool"], data["operation_id"])
            if path.endswith("/save"):
                receipts[key] = data
            if key not in receipts or receipts[key]["input_hash"] != data["input_hash"]:
                return httpx.Response(409)
            return httpx.Response(200, json={"bindings": receipts[key]["bindings"]})
        if path == "/internal/oauth/exchange":
            return httpx.Response(200, json={"credential": "mshod_test"})
        assert req.headers["authorization"] == "Bearer mshod_test"
        if path == "/v1/sdk/templates/457":
            return httpx.Response(200, json={"id": 457, "endpoints": []})
        if path == "/v1/sdk/pods/estimate":
            return httpx.Response(200, json=FakeMeshive.ESTIMATE)
        if path == "/v1/sdk/pods":
            writes.append(req)
            op = req.headers["Idempotency-Key"]
            records[(op, req.method, path)] = {"status": "done", "idempotencyKey": op}
            return httpx.Response(201, json={"name": "demo", "transactionId": 123, "workspace": "0123456789abcdef",
                                             "estimate": FakeMeshive.ESTIMATE, "podName": "pod-a"})
        if path.startswith("/v1/sdk/operations/"):
            lookup = (path.rsplit("/", 1)[1], req.url.params["method"], "/v1/sdk" + req.url.params["path"])
            return httpx.Response(200, json=records[lookup])
        raise AssertionError(req.url)

    monkeypatch.setattr(oauth, "private_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(backend),
                                                                         base_url=settings.oauth_internal_url))
    async def call(name, args):
        response = await http.post("/mcp", headers={**HEADERS, "Authorization": "Bearer mshoa_writer"},
                                   json=_rpc("tools/call", {"name": name, "arguments": args}))
        return response.json()["result"]

    args = {"name": "demo", "template_id": 457, "gpu_model": "RTX 3060", "workspace": "0123456789abcdef"}
    assert not (await call("estimate_pod", args)).get("isError")
    preview = (await call("create_pod", args))["structuredContent"]
    assert preview["confirmed"] is False
    op = preview["operation_id"]
    assert (await call("create_pod", {**args, "gpu_count": 2, "confirm": True, "operation_id": op}))["isError"]
    assert not writes
    result = (await call("create_pod", {**args, "confirm": True, "operation_id": op}))["structuredContent"]
    assert len(writes) == 1 and writes[0].headers["Idempotency-Key"] == op
    assert result["operation_lookup"] == {"method": "POST", "path": "/pods"}
    record = await call("operation_status", {**result["operation_lookup"], "operation_id": op})
    assert record["structuredContent"]["status"] == "done"


def test_oauth_result_redacts_known_secrets_in_text_data_and_errors():
    key = "meshive_" + "a" * 64
    payload = {"log": "client used " + key, "env": [{"is_secret": True, "value": "private-value"}],
               "script": "export HF_TOKEN=hf_private_test", "password": "private-password",
               "headers": "Authorization: Bearer private-bearer", "ports": {8888: "https"},
               "upload_token": "scoped-handle", "url": "https://files.example.test/file?signature=scoped"}
    clean = oauth.public_result(payload)
    assert key not in json.dumps(clean) and "private-" not in json.dumps(clean) and "hf_private_test" not in clean["script"]
    assert clean["upload_token"] == payload["upload_token"] and clean["url"] == payload["url"]
    error = oauth.public_result({"code": "insufficient_credit", "message": "Buy more credits.", "next_step": "top up https://billing.test"})
    assert "http" not in str(error) and "top up" not in str(error)


def test_oauth_redacts_inference_keys_and_webhook_receiver_urls_in_results():
    key = "mk-" + "T" * 40
    payload = {"inferenceKey": "private-key", "signing_secret": "private-secret",
               "request_body": json.dumps({"log": "key " + key, "webhookSigningSecret": "private-signature",
                                           "downloadUrls": ["https://files.test/out?sig=x"]}),
               "last_error": "500 from https://discord.com/api/v10/webhooks/66/private-receiver",
               "message": "Could not reach https://hooks.slack.com/services/T1/B1/private-receiver"}
    clean = oauth.public_result(payload)
    assert "private-" not in json.dumps(clean) and key not in json.dumps(clean)
    assert json.loads(clean["request_body"])["downloadUrls"] == ["https://files.test/out?sig=x"]


async def test_webhook_preview_pins_workspace_when_its_label_moves(private, http, monkeypatch):
    original = "0123456789abcdef"
    current = original
    receipts, writes = [], []

    async def backend(req):
        path = req.url.path
        if path.endswith("/inspect"):
            return httpx.Response(200, json={"scope": "read write"})
        if path.endswith("/previews/save"):
            receipts.append(json.loads(req.content))
            return httpx.Response(200, json={"bindings": receipts[-1]["bindings"]})
        if path.endswith("/previews/check"):
            assert receipts[-1]["input_hash"] == json.loads(req.content)["input_hash"]
            return httpx.Response(200, json={"bindings": receipts[-1]["bindings"]})
        if path.endswith("/exchange"):
            return httpx.Response(200, json={"credential": "mshod_test"})
        if path == "/v1/sdk/workspaces":
            return httpx.Response(200, json=[{"namespaceName": current, "workspaceName": "lab", "memberRole": "admin"}])
        if path == "/v1/sdk/webhooks/event-types":
            return httpx.Response(200, json=[{"type": "request.succeeded"}])
        if path == "/v1/sdk/webhooks" and req.method == "POST":
            writes.append(req.url.params["workspace"])
            return httpx.Response(201, json={"id": 5, **json.loads(req.content)})
        raise AssertionError(req.url)

    monkeypatch.setattr(oauth, "private_http", lambda: httpx.AsyncClient(transport=httpx.MockTransport(backend),
                                                                         base_url=settings.oauth_internal_url))
    async def save(args):
        r = await http.post("/mcp", headers={**HEADERS, "Authorization": "Bearer mshoa_writer"}, json=_rpc(
            "tools/call", {"name": "save_webhook", "arguments": args}))
        assert not r.json()["result"].get("isError"), r.text
        return r.json()["result"]["structuredContent"]

    args = {"workspace": "lab", "url": "https://hooks.example.org/events", "event_types": ["request.succeeded"]}
    preview = await save(args)
    assert preview["workspace"] == original and preview["confirmed"] is False
    current = "aaaaaaaaaaaaaaaa"
    await save({**args, "confirm": True, "operation_id": preview["operation_id"]})
    assert writes == [original]
