from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ..workspace import ALL, needs_input, resolve, resolve_many
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "pods", annotations=READ_ONLY)
    async def pods(ctx: Context[Any, Any], workspace: str | None = None, pod: str | None = None,
                   status: str | None = None, include_metrics: bool = False,
                   limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the pods (GPU/CPU containers) in a workspace, or show one pod by its `pod_name`.
        `workspace` takes the id from the workspaces tool (a label also works); pass "all" to list pods across every workspace, or omit it if the user has only one.
        Set `include_metrics` for live CPU/RAM/GPU usage of a single pod; use `status` to filter the list."""
        async with meshive_client(ctx) as client:
            if pod is not None:
                ws = await resolve(client, workspace)
                if needs_input(ws):
                    return ws
                item = to_dict(await call(client.get_pod, pod, ws))
                if include_metrics:
                    item["metrics"] = to_dict(await call(client.get_pod_metrics, pod, ws))
                return {"pod": item}
            targets = await resolve_many(client, workspace)
            if needs_input(targets):
                return targets
            items = []
            for ws in targets:
                items.extend(await call(client.list_pods, ws))
        if status:
            items = [p for p in items if p.status.lower() == status.lower()]
        page = paginate([to_dict(p) for p in items], limit, cursor)
        page["workspace"] = ALL if len(targets) > 1 else targets[0]
        return page
