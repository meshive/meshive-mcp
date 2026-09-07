from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import clamp_limit, decode_cursor, encode_cursor
from ..serialize import to_dict
from ..workspace import needs_input, resolve
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "assets", annotations=READ_ONLY)
    async def assets(ctx: Context[Any, Any], workspace: str | None = None, asset: str | None = None,
                     asset_type: str | None = None, status: str | None = None,
                     limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the Asset Hub assets (datasets, models, adapters, outputs) of a workspace, or show one by `asset_id` ("asset_...").
        The list also reports managed storage usage and the estimated monthly storage cost in USD.
        Filter with `asset_type` or `status`. `workspace` takes the id from the workspaces tool (a label also works); omit it if the user has only one."""
        async with meshive_client(ctx) as client:
            if asset is not None:
                return {"asset": to_dict(await call(client.get_asset, asset))}
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            size = clamp_limit(limit)
            offset = decode_cursor(cursor)
            # 백엔드는 page/pageSize. 커서 offset 은 항상 size 의 배수로만 발급되므로 page 로 환산된다.
            page_no = offset // size + 1
            page = await call(client.list_assets, ws, asset_type=asset_type, status=status,
                              page=page_no, page_size=size)
            storage = await call(client.get_asset_storage, ws)
        next_offset = offset + len(page.items)
        return {
            "items": [to_dict(a) for a in page.items],
            "total": page.total,
            "next_cursor": encode_cursor(next_offset) if next_offset < page.total else None,
            "storage": to_dict(storage),
            "workspace": ws,
        }
