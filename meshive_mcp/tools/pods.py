from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ..workspace import needs_input, resolve
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "pods", annotations=READ_ONLY)
    async def pods(ctx: Context[Any, Any], workspace: str | None = None, pod: str | None = None,
                   status: str | None = None, include_metrics: bool = False,
                   limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the pods (GPU/CPU containers) in a workspace, or show one pod by its `pod_name`.
        Omit `workspace` if the user has only one; otherwise pass the id from the workspaces tool.
        Set `include_metrics` for live CPU/RAM/GPU usage of a single pod; use `status` to filter the list."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if pod is not None:
                item = to_dict(await call(client.get_pod, pod, ws))
                if include_metrics:
                    item["metrics"] = to_dict(await call(client.get_pod_metrics, pod, ws))
                return {"pod": item}
            items = await call(client.list_pods, ws)
        if status:
            items = [p for p in items if p.status.lower() == status.lower()]
        page = paginate([to_dict(p) for p in items], limit, cursor)
        page["workspace"] = ws
        return page
