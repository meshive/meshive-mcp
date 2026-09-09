"""읽기 도구 — 로그 (read 스코프). 계약 §1 `logs`."""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..errors import invalid_argument
from ..serialize import to_dict
from ..workspace import needs_input, resolve, resolve_pod
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "logs", annotations=READ_ONLY)
    async def logs(ctx: Context[Any, Any], pod: str | None = None, task: str | None = None,
                   workspace: str | None = None, tail: int = 200, wait: float | None = None,
                   container: str | None = None) -> dict[str, Any]:
        """Read the last `tail` lines (≤1000) of a pod's or a task's logs — use this to see why something is not working.
        Pass `pod` (pod_name or its display name, needs `workspace`) or `task` (task_id). If nothing is buffered yet the server starts a log
        watcher and waits up to `wait` seconds (default 8); a `none` source with a note means the pod produced no output.
        Output is capped at 64 KB (`truncated` says so) and task scripts need print(..., flush=True) to show anything."""
        if bool(pod) == bool(task):
            raise invalid_argument("Pass exactly one of `pod` or `task`.")
        async with meshive_client(ctx) as client:
            if task:
                result = await call(client.get_task_logs, task, tail=tail, wait=wait)
            else:
                ws = await resolve(client, workspace)
                if needs_input(ws):
                    return ws
                pod = await resolve_pod(client, ws, pod)
                result = await call(client.get_pod_logs, pod, ws, tail=tail, container=container, wait=wait)
        out = to_dict(result)
        out["text"] = result.text
        return out
