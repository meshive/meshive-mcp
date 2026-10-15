"""CPU lines a CPU pod can run on, with per-core and per-GiB RAM prices. Needs an API key (live stock)."""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..errors import invalid_argument
from ..serialize import to_dict
from ._common import READ_ONLY, call, meshive_tool
from .gpus import RENTAL_TYPES


def register(server: MCPServer) -> None:
    @meshive_tool(server, "cpus", title="List CPU Lines", annotations=READ_ONLY)
    async def cpus(ctx: Context[Any, Any], rental_type: str = "demand") -> dict[str, Any]:
        """Show the CPU lines a CPU pod can run on right now, with hourly USD prices per vCPU core and per GiB of RAM.
        A line is a CPU vendor, product line and generation with a fixed RAM type (`ddr_type`, 0 = unknown). Pass its
        `cpu_line` to estimate_pod or create_pod; without one the cheapest line that fits is used.
        `max_vcpus_per_pod` and `max_ram_per_pod` (MiB) are the most one pod can get on a single machine.
        Use `rental_type` "spot" for the cheaper preemptible tier."""
        rental_type = (rental_type or "demand").lower()
        if rental_type not in RENTAL_TYPES:
            raise invalid_argument("rental_type must be 'demand' or 'spot'.", allowed=list(RENTAL_TYPES))
        async with meshive_client(ctx) as client:
            items = await call(client.list_cpus, rental_type=rental_type)
        rows = [to_dict(line) for line in items]
        return {"items": rows, "total": len(rows)}
