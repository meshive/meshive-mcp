from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import clamp_limit, decode_cursor, encode_cursor
from ..serialize import to_dict
from ..workspace import needs_input, resolve
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "tasks", annotations=READ_ONLY)
    async def tasks(ctx: Context[Any, Any], workspace: str | None = None, task: str | None = None,
                    status: str | None = None, limit: int | None = None,
                    cursor: str | None = None) -> dict[str, Any]:
        """List serverless tasks (one-off GPU jobs) in a workspace, or show one by `task_id` (looks like "task_...").
        Filter with `status` (queued, running, succeeded, failed, ...); the detail view includes cost and exit code.
        Omit `workspace` if the user has only one workspace."""
        async with meshive_client(ctx) as client:
            if task is not None:
                return {"task": to_dict(await call(client.get_task, task))}
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            size = clamp_limit(limit)
            offset = decode_cursor(cursor)
            # 백엔드가 limit/offset 을 지원하는 유일한 목록 — 한 장 더 받아 다음 페이지 유무를 안다.
            items = await call(client.list_tasks, ws, status=status, limit=size + 1, offset=offset)
        has_more = len(items) > size
        items = items[:size]
        return {
            "items": [to_dict(t) for t in items],
            "next_cursor": encode_cursor(offset + size) if has_more else None,
            "workspace": ws,
        }
