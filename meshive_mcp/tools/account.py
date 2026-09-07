from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..serialize import to_dict
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "account", annotations=READ_ONLY)
    async def account(ctx: Context[Any, Any]) -> dict[str, Any]:
        """Show who the configured API key belongs to and the account's credit balance.
        Call this first when the user asks about their account, balance, or which key is in use.
        Report the balance in USD; mention auto-recharge only if it is enabled."""
        async with meshive_client(ctx) as client:
            me = await call(client.me)
            credit = await call(client.get_credit)
        return {"user": to_dict(me), "credit": to_dict(credit)}
