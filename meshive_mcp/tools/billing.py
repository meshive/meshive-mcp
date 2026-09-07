from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..errors import invalid_argument
from ..paging import paginate
from ..serialize import to_dict
from ._common import READ_ONLY, call, meshive_tool

KINDS = ("credit", "earnings")


def register(server: MCPServer) -> None:
    @meshive_tool(server, "billing_history", annotations=READ_ONLY)
    async def billing_history(ctx: Context[Any, Any], kind: str = "credit", since: str | None = None,
                              until: str | None = None, limit: int | None = None,
                              cursor: str | None = None) -> dict[str, Any]:
        """Show money movements: `kind` "credit" lists top-ups and refunds; "earnings" shows host payouts per day.
        `since` and `until` are dates in YYYY-MM-DD; amounts are USD and refunds are negative.
        For the current balance use the account tool instead."""
        kind = (kind or "credit").lower()
        if kind not in KINDS:
            raise invalid_argument("kind must be 'credit' or 'earnings'.", allowed=list(KINDS))
        async with meshive_client(ctx) as client:
            if kind == "credit":
                items = await call(client.list_credit_history, start_date=since, end_date=until)
                return paginate([to_dict(e) for e in items], limit, cursor)
            earnings = await call(client.get_earnings, start_date=since, end_date=until)
        page = paginate([to_dict(d) for d in earnings.history], limit, cursor)
        page["summary"] = {
            "current_hourly_usd": earnings.current_hourly,
            "daily_usd": earnings.daily,
            "accumulated_until_payout_usd": earnings.accumulated_until_payout,
        }
        return page
