"""OAuth admission and request-scoped SDK transport for the remote MCP endpoint."""
from __future__ import annotations

from contextvars import ContextVar
import hashlib
import json
import re
import uuid
import ssl
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from meshive._config import resolve_base_url
from starlette.responses import JSONResponse

from .serialize import mask_webhook_url, secret_webhook_prefix
from .settings import settings

READ = frozenset("""gpus cpus account activity workspaces billing_history pods storages estimate_pod transactions
watched_folders templates model_sets models servings detect_model source_credentials tasks estimate_task
operation_status assets asset_files buckets download_links access_keys machines machine_history host_messages logs
pod_history inference_requests inference_keys webhooks""".split())
current_tool: ContextVar[str] = ContextVar("meshive_oauth_tool")
action_inputs: ContextVar[tuple[bool, str | None]] = ContextVar("meshive_action_inputs", default=(False, None))
ACTION_PATHS = {
    "pods": ("GET", r"/pods/[^/]+"),
    "ssh_access": ("POST", r"/pods/[^/]+/ssh"),
    "connect_storage": ("POST", r"/storages/[^/]+/connect"),
    "machine_credentials": ("POST", r"/machines/[^/]+/credentials"),
    "host_install_command": ("POST", r"/host/install-command"),
    "create_access_key": ("POST", r"/pods/[^/]+/ports/[0-9]+/keys"),
    "roll_access_key": ("POST", r"/pods/[^/]+/ports/[0-9]+/keys/[0-9]+/roll"),
    "create_inference_key": ("POST", r"/inference-keys"),
    "roll_inference_key": ("POST", r"/inference-keys/[1-9][0-9]*/roll"),
}
CONSOLE = frozenset(ACTION_PATHS) - {"pods"}  # Pod details require the console only with show_secrets.


class ConsoleConfirmation(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__("Complete the operation in the Meshive console.")


NO_STORE = {"Cache-Control": "no-store"}


def rental_mode(value):
    return (value or "demand").lower() if isinstance(value, str) or value is None else None


def console_only(name, args):
    value = args.get("show_secrets", False)
    return name in CONSOLE or (name == "pods" and str(value).lower() not in {"false", "0", "off", "no", "n", "f"})


def required_scope(name, args):
    return "read write" if name not in READ or console_only(name, args) else "read"


def schemes(name):
    entries = [{"type": "oauth2", "scopes": required_scope(name, {}).split()}]
    return [{"type": "noauth"}, *entries] if name == "gpus" else entries


def metadata():
    return {"resource": settings.oauth_resource, "authorization_servers": [settings.oauth_issuer],
            "scopes_supported": ["read", "write"], "bearer_methods_supported": ["header"]}


def denied(status, scope, rpc, error):
    base = settings.oauth_resource.removesuffix("/mcp")
    description = "Grant the requested Meshive permissions." if status == 403 else "Sign in to Meshive to continue."
    challenge = (f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource/mcp", scope="{scope}", '
                 f'error="{error}", error_description="{description}"')
    body = {"jsonrpc": "2.0", "id": rpc.get("id")}
    if rpc.get("method") == "tools/call":
        body["result"] = {"isError": True, "content": [{"type": "text", "text": description}],
                          "_meta": {"mcp/www_authenticate": [challenge]}}
    else:
        body["error"] = {"code": -32001, "message": description}
    # Clients that request OpenAI tool presentation consume a successful RPC carrying a tool error.
    # This hint selects the response format only. Admission has already denied the operation.
    params = rpc.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    as_tool = "result" in body and isinstance(meta, dict) and any(k in meta for k in ("openai/locale", "openai/userAgent"))
    return JSONResponse(body, 200 if as_tool else status, {**NO_STORE, "WWW-Authenticate": challenge})


def private_http():
    if not settings.oauth_internal_url or urlsplit(settings.oauth_internal_url).scheme != "https":
        raise ValueError("Missing secure authorization service URL")
    root = Path(settings.oauth_tls_dir)
    ctx = ssl.create_default_context(cafile=str(root / "ca.crt"))
    ctx.load_cert_chain(str(root / "tls.crt"), str(root / "tls.key"))
    return httpx.AsyncClient(base_url=settings.oauth_internal_url, verify=ctx, trust_env=False,
                             follow_redirects=False, timeout=settings.request_timeout)


async def admit(request, rpc):
    if not isinstance(rpc, dict):
        return JSONResponse({"error": "invalid_request"}, 400)
    params = rpc.get("params")
    params = params if isinstance(params, dict) else {}
    call = rpc.get("method") == "tools/call"
    name = params.get("name") if call else None
    name = name if isinstance(name, str) else ""
    args = params.get("arguments")
    args = args if isinstance(args, dict) else {}
    need = required_scope(name, args) if call else "read"
    header = request.headers.getlist("authorization")
    if not header:
        if call and not (name == "gpus" and rental_mode(args.get("rental_type")) in ("demand", "spot")):
            return denied(401, need, rpc, "invalid_token")
        return None
    prefix, _, token = header[0].partition(" ")
    if len(header) != 1 or prefix.lower() != "bearer" or len(token) > 512:
        return denied(401, need, rpc, "invalid_token")
    try:
        if token.startswith("meshive_"):
            async with httpx.AsyncClient(trust_env=False, timeout=settings.request_timeout) as client:
                result = await client.get(resolve_base_url(settings.base_url) + "/v1/sdk/me", headers={"Authorization": header[0]})
        elif token.startswith("mshoa_"):
            async with private_http() as client:
                result = await client.post("/internal/oauth/inspect", headers={"Authorization": header[0]})
            if result.status_code == 200 and not set(need.split()) <= set(result.json()["scope"].split()):
                return denied(403, need, rpc, "insufficient_scope")
        else:
            return denied(401, need, rpc, "invalid_token")
        if result.status_code == 401:
            return denied(401, need, rpc, "invalid_token")
        if result.status_code == 429:
            return JSONResponse({"error": "rate_limited"}, 429, {**NO_STORE, "Retry-After": result.headers.get("Retry-After", "1")})
        result.raise_for_status()
    except (httpx.HTTPError, OSError, ValueError, KeyError):
        return JSONResponse({"error": "authorization_service_unavailable"}, 503, {**NO_STORE, "Retry-After": "2"})
    return None


class DelegatedTransport(httpx.AsyncBaseTransport):
    def __init__(self, http, token, tool):
        self.http = http
        self.token = token
        self.tool = tool

    async def handle_async_request(self, request):
        target = request.url.raw_path.decode("ascii")
        if request.url.copy_with(path="/", query=None) != httpx.URL(settings.oauth_internal_url).copy_with(path="/", query=None):
            raise httpx.RequestError("Unsupported operation destination", request=request)
        if request.method == "GET" and target == "/v1/capabilities":
            # This versioned public API also reports operational feature flags for ports and assets.
            async with httpx.AsyncClient(trust_env=False, timeout=settings.request_timeout) as public:
                return await public.get(resolve_base_url(settings.base_url) + "/v1/capabilities")
        if not target.startswith("/v1/sdk/"):
            raise httpx.RequestError("Unsupported operation destination", request=request)
        data = await request.aread()
        operation = request.headers.get("Idempotency-Key")
        sensitive, op = action_inputs.get()
        spec = ACTION_PATHS.get(self.tool) if sensitive else None
        if spec and request.method == spec[0] and re.fullmatch("/v1/sdk" + spec[1], request.url.path):
            prepared = await self.http.post("/internal/oauth/actions", headers={"Authorization": "Bearer " + self.token},
                json={"tool": self.tool, "method": request.method, "target": target,
                      "body": json.loads(data) if data else {}, "operation_id": op or str(uuid.uuid4())})
            if prepared.status_code != 200:
                return httpx.Response(prepared.status_code, json={"message": "Could not prepare console confirmation."})
            info = prepared.json()
            # Only the public action link and status cross the tool boundary.
            raise ConsoleConfirmation({key: info[key] for key in ("console_url", "status", "operation_id")})
        if spec and request.method != "GET":
            # Sensitive tools can delegate prerequisite reads. A changed issuing route must be reviewed in the console.
            raise httpx.RequestError("Unsupported console confirmation operation", request=request)
        binding = {}
        upload = request.headers.get("X-Asset-Upload-Token")
        if upload and re.fullmatch(r"/v1/sdk/assets/[^/]+/uploads/[^/]+/(?:complete|abort|part-urls)", request.url.path):
            binding["upload_token_hash"] = hashlib.sha256(upload.encode()).hexdigest()
        exchange = await self.http.post("/internal/oauth/exchange", headers={"Authorization": "Bearer " + self.token},
            json={"method": request.method, "target": target, "body_hash": hashlib.sha256(data).hexdigest(),
                  "tool": self.tool, "operation_id": operation, **binding})
        if exchange.status_code != 200:
            return httpx.Response(exchange.status_code, json={"message": "The operation could not be authorized."})
        credential = exchange.json()["credential"]
        headers = {"Authorization": "Bearer " + credential}
        if binding:
            headers["X-Asset-Upload-Token"] = upload
        for key in ("Idempotency-Key", "Content-Type", "User-Agent", "X-Meshive-Client"):
            if key in request.headers:
                headers[key] = request.headers[key]
        return await self.http.request(request.method, request.url, headers=headers, content=data)

def tool_list_sender(send):
    """Add wire extensions after SDK protocol projection; the JSON response is bounded by our registered tools."""
    chunks = bytearray()
    async def send_list(message):
        if message["type"] != "http.response.body":
            await send(message)
            return
        chunks.extend(message.get("body", b""))
        if message.get("more_body"):
            return
        try:
            body = json.loads(chunks)
            for tool in body["result"]["tools"]:
                entries = schemes(tool["name"])
                tool["securitySchemes"] = entries
                tool.setdefault("_meta", {})["securitySchemes"] = entries
                if tool["name"] in ACTION_PATHS:
                    tool["description"] += (" With OAuth, credential values are revealed only in the signed-in Meshive "
                                            "console. Give the user the returned console_url to complete the operation.")
        except (ValueError, KeyError, TypeError):
            await send({**message, "body": bytes(chunks)})
            return
        await send({**message, "body": json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()})
    return send_list


_TOKEN_TEXT = re.compile(r"(?<![A-Za-z0-9_])(?:meshive_[0-9a-fA-F]{64}|msho[ar]_[A-Za-z0-9_-]{43}|mshod_[A-Za-z0-9_=-]{32,}|mshv_[A-Za-z0-9_-]{16,}|mk-[A-Za-z0-9_-]{32,})")
_URL_TEXT = re.compile(r"""https?://[^\s"'<>]+""", re.IGNORECASE)
_BEARER_TEXT = re.compile(r"(?i)(\bAuthorization\s*:\s*Bearer\s+)[A-Za-z0-9._~+/=-]+")
_ASSIGNED_SECRET = re.compile(r"""(?im)(\b(?:HF_TOKEN|OPENAI_API_KEY|AWS_SECRET_ACCESS_KEY|ACCESS_PASSWORD|CLIENT_SECRET|API_KEY|PASSWORD)\s*=\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s;]+)""")
_WEBHOOK_SECRET_TEXT = re.compile(
    r'(?i)("(?:inference_?key|(?:webhook_?)?signing_?secret|webhook_?secret)"\s*:\s*)"(?:\\.|[^"\\])*"')
_SECRET_FIELDS = {"password", "passwd", "apikey", "accesstoken", "refreshtoken", "clientsecret", "authorization",
                  "privatekey", "accesskey", "inferencekey", "signingsecret", "webhooksecret", "webhooksigningsecret"}


def _mask_receiver(match):
    url = match[0]
    return mask_webhook_url(url)[0] if secret_webhook_prefix(url) else url


def public_result(result):
    """Redact known credentials before creating either representation of an OAuth tool result."""
    def clean(value):
        if isinstance(value, dict):
            marked = value.get("is_secret") is True or value.get("isSecret") is True
            cleaned = {}
            for key, item in value.items():
                sensitive = isinstance(key, str) and key.replace("_", "").lower() in _SECRET_FIELDS
                cleaned[key] = "[redacted]" if sensitive or (marked and key == "value") else clean(item)
            return cleaned
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            # Error messages, logs and webhook deliveries can repeat the receiver URL inside text.
            value = _URL_TEXT.sub(_mask_receiver, value)
            value = _WEBHOOK_SECRET_TEXT.sub(r'\1"[redacted]"', value)
            value = _BEARER_TEXT.sub(r"\1[redacted]", value)
            return _ASSIGNED_SECRET.sub(r"\1[redacted]", _TOKEN_TEXT.sub("[redacted]", value))
        return value

    if result.get("code") in {"insufficient_credit", "asset_usage_blocked"}:
        result = {**result, "message": "The account balance is insufficient for this operation.",
                  "next_step": "Report the balance requirement to the user. Do not retry automatically."}
    return clean(result)
