from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ..workspace import ALL, needs_input, resolve_many
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "transactions", annotations=READ_ONLY)
    async def transactions(ctx: Context[Any, Any], workspace: str | None = None,
                           limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """Show the pod operations still in flight, and why one is taking so long.
        A pod that stays `creating` is usually pulling its image, not stuck: `step` names the current
        stage and `progress` is 0..1 for stages that report it (null means the stage reports no
        progress — do NOT show it as 0%). `detail` carries the failure reason when one is known.
        Finished work drops off this list, so an empty result means "nothing in flight" and never
        "nothing failed"; check the pod's own status for that.
        `workspace` takes the id from the workspaces tool (a label also works); pass "all" for every workspace, or omit it if the user has only one."""
        async with meshive_client(ctx) as client:
            targets = await resolve_many(client, workspace)
            if needs_input(targets):
                return targets
            items = []
            for ws in targets:
                items.extend(await call(client.list_transactions, ws))
        page = paginate([to_dict(t) for t in items], limit, cursor)
        page["workspace"] = ALL if len(targets) > 1 else targets[0]
        return page
