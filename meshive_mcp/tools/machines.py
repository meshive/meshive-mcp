from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "machines", title="List Hosted Machines", annotations=READ_ONLY)
    async def machines(ctx: Context[Any, Any], machine: str | None = None, include_metrics: bool = False,
                       limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """For users who host hardware on Meshive: list their machines, or show one by `machine_id`.
        Items include online status, GPU model and count, hourly earnings in USD, uptime rate (0..1) and
        `unread_messages`, the messages from the workspaces using the machine that the host hasn't read (read them with
        host_messages).
        One machine also shows `clients`: other users' workspaces that use it, with their pods and storage volumes
        (stopped ones too; a pod's `ram_request` is MiB, a volume's `capacity_gb` and `available_size_gb` are GiB and
        `usage_rate` is 0..1). Their `workspace` ids are what send_host_message takes. It also shows `maintenance` (every
        window with its `id` and `status`: upcoming, overdue, in_progress, done or expired), the `schedules` summary,
        the diagnostics `boot_report` and `network_warnings`, and `fee` (`platform_fee` as a fraction, 0.2 = 20%, and
        when a promotional fee ends).
        Set `include_metrics` for a single machine's live CPU/RAM/GPU/network usage. In `metrics`, `ram_size` is bytes,
        the other sizes (`ram_allocated`, volume sizes, GPU `vram_size`) are MiB, and `network_receive`/`network_transmit`
        are bytes per second (x 8 / 1,000,000 for Mbps)."""
        async with meshive_client(ctx) as client:
            if machine is not None:
                item = to_dict(await call(client.get_machine, machine))
                item["fee"] = {"platform_fee": item.pop("platform_fee", None),
                               "promotional_fee_until": item.pop("promotional_fee_until", None)}
                try:
                    item["maintenance"] = [to_dict(w) for w in await call(client.list_maintenance, machine)]
                except ToolError as exc:
                    # The machine was found a moment ago, so not_found here means a server without maintenance
                    # windows on the API key surface; the detail is still worth returning.
                    if json.loads(str(exc)).get("code") != "not_found":
                        raise
                if include_metrics:
                    item["metrics"] = to_dict(await call(client.get_machine_metrics, machine))
                return {"machine": item}
            items = await call(client.list_machines)
        return paginate([to_dict(m) for m in items], limit, cursor)
