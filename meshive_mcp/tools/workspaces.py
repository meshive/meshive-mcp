from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..serialize import to_dict
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "workspaces", annotations=READ_ONLY)
    async def workspaces(ctx: Context[Any, Any], workspace: str | None = None) -> dict[str, Any]:
        """List the user's workspaces, or show one workspace in detail with its cost summary and members.
        Omit `workspace` to list; pass a workspace id (the `namespace_name` from the list) for details.
        Other tools take the same `workspace` id, so use this to find it when the user names a workspace by label."""
        async with meshive_client(ctx) as client:
            if workspace is None:
                items = await call(client.list_workspaces)
                return {"items": [to_dict(w) for w in items], "total": len(items)}
            detail = await call(client.get_workspace, workspace)
            members = await call(client.list_members, workspace)
        return {"workspace": to_dict(detail), "members": [to_dict(m) for m in members]}
