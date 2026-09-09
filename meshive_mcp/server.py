"""MCPServer 조립 + HTTP 앱.

무상태(stateless) + JSON 응답 모드: 세션도 SSE 스트림도 없다. 서버는 평범한 HTTP 서비스가 되고
수평 확장·재시작이 자유롭다. 대가는 서버→클라이언트 알림을 못 보내는 것인데, 도구만 쓰는 v1 은
필요 없다.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .settings import settings
from .tools import register_all

INSTRUCTIONS = """Meshive is a GPU cloud: users rent GPU/CPU pods, storage volumes, serverless model \
deployments (servings) and one-off GPU jobs (tasks), and can host their own machines.

Authentication: every tool except `gpus` needs the user's Meshive API key sent as \
'Authorization: Bearer meshive_...' by the MCP client. If a tool answers with code `no_api_key` \
or `invalid_api_key`, follow its `next_step` and do not retry until the user confirms.

Workspaces: most resources live in a workspace. If `workspace` is omitted and the user has several, \
the tool returns `needs_input: "workspace"` with the list — ask the user, then call again.

Lists are paged: pass `next_cursor` back as `cursor` to continue. Never assume a list is complete \
when `next_cursor` is not null.

Prices are USD per hour unless the field name says otherwise. When you show costs, state the hourly \
price and, if relevant, the running total. Errors carry `code`, `message` and `next_step`; \
follow `next_step` literally, especially the waiting instructions on `rate_limited`.

Spending and deleting: create_pod, create_storage, deploy_serving, submit_task, delete_pod, delete_storage and \
delete_serving take `confirm`. With confirm=false they only return an estimate or a summary and change nothing. \
Show that to the user, get an explicit yes, and only then call again with confirm=true. Never set confirm=true \
without the user's go-ahead in this conversation. Write tools need an API key with the write scope; if you get \
`write_scope_required`, tell the user how to issue one. Changes are asynchronous: after an accepted call, poll the \
matching list tool for the new status instead of assuming it."""


class RefuseStreams:
    """`GET /mcp`(standalone SSE)와 `DELETE /mcp`(세션 종료)를 405 로 거절한다.

    SDK 는 무상태 모드에서도 GET 에 SSE 스트림을 열어 연결을 붙잡는다. 알림을 보내지 않는
    서버가 스트림을 열어 두면 로드밸런서 idle 커넥션만 쌓인다. 스펙은 405 를 허용한다.
    """

    def __init__(self, app: ASGIApp, path: str = "/mcp") -> None:
        self.app = app
        self.path = path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == self.path and scope["method"] in ("GET", "DELETE"):
            response = PlainTextResponse("Method Not Allowed", status_code=405, headers={"Allow": "POST"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_server() -> MCPServer:
    server = MCPServer(
        name="meshive",
        title="Meshive GPU Cloud",
        version=__version__,
        instructions=INSTRUCTIONS,
        website_url="https://meshive.ai",
    )
    register_all(server)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    return server


def create_http_app(server: MCPServer | None = None) -> Starlette:
    server = server or create_server()
    if settings.allowed_hosts:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                             allowed_hosts=settings.allowed_hosts)
    else:
        # ingress 뒤에서는 Host 가 공인 도메인이라 기본 검사가 요청을 막는다. 보호는 ingress 가 맡는다.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=security,
    )
    app.add_middleware(RefuseStreams)
    return app
