from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "machines", annotations=READ_ONLY)
    async def machines(ctx: Context[Any, Any], machine: str | None = None, include_metrics: bool = False,
                       limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """For users who host hardware on Meshive: list their machines, or show one by `machine_id`.
        Items include online status, GPU model and count, hourly earnings in USD, and uptime rate (0..1).
        Set `include_metrics` for a single machine's live CPU/RAM/GPU/network usage."""
        async with meshive_client(ctx) as client:
            if machine is not None:
                item = to_dict(await call(client.get_machine, machine))
                if include_metrics:
                    item["metrics"] = to_dict(await call(client.get_machine_metrics, machine))
                return {"machine": item}
            items = await call(client.list_machines)
        return paginate([to_dict(m) for m in items], limit, cursor)
