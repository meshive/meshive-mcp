from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ..workspace import needs_input, resolve
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "storages", annotations=READ_ONLY)
    async def storages(ctx: Context[Any, Any], workspace: str | None = None, storage: str | None = None,
                       limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the storage volumes in a workspace, or show one volume by its `pv_name`.
        Sizes are in GB and `usage_rate` is 0..1; `linked_pods` tells which pods mount the volume.
        Omit `workspace` if the user has only one workspace."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if storage is not None:
                return {"storage": to_dict(await call(client.get_storage, storage, ws))}
            items = await call(client.list_storages, ws)
        page = paginate([to_dict(s) for s in items], limit, cursor)
        page["workspace"] = ws
        return page
