from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ..workspace import ALL, needs_input, resolve, resolve_many
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "storages", annotations=READ_ONLY)
    async def storages(ctx: Context[Any, Any], workspace: str | None = None, storage: str | None = None,
                       limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the storage volumes in a workspace, or show one volume by its `pv_name`.
        Sizes are in GB and `usage_rate` is 0..1; `linked_pods` tells which pods mount the volume.
        `workspace` takes the id from the workspaces tool (a label also works); pass "all" for every workspace, or omit it if the user has only one."""
        async with meshive_client(ctx) as client:
            if storage is not None:
                ws = await resolve(client, workspace)
                if needs_input(ws):
                    return ws
                return {"storage": to_dict(await call(client.get_storage, storage, ws))}
            targets = await resolve_many(client, workspace)
            if needs_input(targets):
                return targets
            items = []
            for ws in targets:
                items.extend(await call(client.list_storages, ws))
        page = paginate([to_dict(s) for s in items], limit, cursor)
        page["workspace"] = ALL if len(targets) > 1 else targets[0]
        return page
