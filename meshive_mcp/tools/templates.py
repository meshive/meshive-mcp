from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "templates", annotations=READ_ONLY)
    async def templates(ctx: Context[Any, Any], workspace: str | None = None, template: int | None = None,
                        app_type: str | None = None, hardware_type: str | None = None,
                        limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the pod templates the user can launch from (official ones, plus custom ones of a workspace), or show one by `template_id`.
        Pass `workspace` to include that workspace's custom templates; without it only official templates are listed.
        Filter with `app_type` (ide, framework, custom, ...) or `hardware_type` (gpu, cpu, any)."""
        async with meshive_client(ctx) as client:
            if template is not None:
                return {"template": to_dict(await call(client.get_template, template, workspace))}
            items = await call(client.list_templates, workspace, app_type=app_type)
        if hardware_type:
            items = [t for t in items if t.hardware_type.lower() in (hardware_type.lower(), "any")]
        return paginate([to_dict(t) for t in items], limit, cursor)
