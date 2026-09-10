from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ..workspace import ALL, needs_input, resolve_many
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "servings", annotations=READ_ONLY)
    async def servings(ctx: Context[Any, Any], workspace: str | None = None, serving: int | None = None,
                       limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the serverless model deployments (servings) in a workspace, or show one by `serving_id`.
        Each item has replica counts, status, the inference `endpoint_url`, and the current hourly price in USD.
        `workspace` takes the id from the workspaces tool (a label also works); pass "all" for every workspace, or omit it if the user has only one."""
        async with meshive_client(ctx) as client:
            if serving is not None:
                return {"serving": to_dict(await call(client.get_serving, serving))}
            targets = await resolve_many(client, workspace)
            if needs_input(targets):
                return targets
            items = []
            for ws in targets:
                items.extend(await call(client.list_servings, ws))
        page = paginate([to_dict(s) for s in items], limit, cursor)
        page["workspace"] = ALL if len(targets) > 1 else targets[0]
        return page
