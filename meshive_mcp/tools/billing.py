from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from .. import money
from ..client import meshive_client
from ..errors import invalid_argument
from ..paging import paginate
from ..serialize import to_dict
from ._common import READ_ONLY, call, meshive_tool

KINDS = ("credit", "earnings", "payouts")


def register(server: MCPServer) -> None:
    @meshive_tool(server, "billing_history", title="Show Billing History", annotations=READ_ONLY)
    async def billing_history(ctx: Context[Any, Any], kind: str = "credit", since: str | None = None,
                              until: str | None = None, limit: int | None = None,
                              cursor: str | None = None) -> dict[str, Any]:
        """Show money movements: `kind` "credit" lists top-ups and refunds; "earnings" shows host earnings per day;
        "payouts" lists the payouts of a host's earnings (the last 90 days by default), with a `summary` of the payout
        account: whether it exists and is fully onboarded, and the earnings waiting to be paid out.
        `since` and `until` are dates in YYYY-MM-DD; amounts are USD and refunds are negative.
        Payouts are sent on their own once enough is waiting; setting up the payout account and its Stripe dashboard
        are in the console (Earnings), not here. For the current balance use the account tool instead."""
        kind = (kind or "credit").lower()
        if kind not in KINDS:
            raise invalid_argument("kind must be 'credit', 'earnings' or 'payouts'.", allowed=list(KINDS))
        async with meshive_client(ctx) as client:
            if kind == "credit":
                items = await call(client.list_credit_history, start_date=since, end_date=until)
                return paginate([to_dict(e) for e in items], limit, cursor)
            if kind == "payouts":
                payouts = await call(client.list_payouts, start_date=since, end_date=until)
                account = await call(client.get_payout_account)
                page = paginate([to_dict(p) for p in payouts], limit, cursor)
                page["summary"] = {
                    "has_account": account.has_account,
                    "is_fully_onboarded": account.is_fully_onboarded,
                    "account_status": account.account_status,
                    "pending_usd": account.pending_amount,
                    "pending_usd_display": money.usd(account.pending_amount),
                    "minimum_payout_usd": account.minimum_payout,
                    "minimum_payout_usd_display": money.usd(account.minimum_payout),
                }
                if not account.has_account or not account.is_fully_onboarded:
                    page["next_step"] = ("Earnings can't be paid out until the user finishes setting up their payout "
                                         "account in the console (Earnings). Tell them; it can't be done here.")
                return page
            earnings = await call(client.get_earnings, start_date=since, end_date=until)
        page = paginate([to_dict(d) for d in earnings.history], limit, cursor)
        page["summary"] = {
            "current_hourly_usd": earnings.current_hourly,
            "daily_usd": earnings.daily,
            "accumulated_until_payout_usd": earnings.accumulated_until_payout,
        }
        return page
