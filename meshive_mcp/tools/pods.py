from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ..workspace import ALL, needs_input, resolve, resolve_many, resolve_pod
from ._common import READ_ONLY, call, meshive_tool


def _is_system_pod(p: Any) -> bool:
    # SDK 0.0.7 Pod 모델에는 필드가 없어 raw 를 본다. 0.0.8 에서 `is_downloader` 필드로 승격 예정.
    return bool(getattr(p, "raw", {}).get("isDownloader"))


def _pod_dict(p: Any) -> dict[str, Any]:
    d = to_dict(p)
    d["is_system"] = _is_system_pod(p)
    return d


def register(server: MCPServer) -> None:
    @meshive_tool(server, "pods", annotations=READ_ONLY)
    async def pods(ctx: Context[Any, Any], workspace: str | None = None, pod: str | None = None,
                   status: str | None = None, include_metrics: bool = False, include_system: bool = False,
                   limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the pods (GPU/CPU containers) in a workspace, or show one pod by its `pod_name` (its display name also works).
        `workspace` takes the id from the workspaces tool (a label also works); pass "all" to list pods across every workspace, or omit it if the user has only one.
        Set `include_metrics` for live CPU/RAM/GPU usage of a single pod; use `status` to filter the list. System-managed downloader pods are hidden unless `include_system` is true; they are free and cannot be stopped or deleted."""
        async with meshive_client(ctx) as client:
            if pod is not None:
                ws = await resolve(client, workspace)
                if needs_input(ws):
                    return ws
                pod = await resolve_pod(client, ws, pod)
                item = _pod_dict(await call(client.get_pod, pod, ws))
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
        if not include_system:
            # 웹 콘솔과 동일하게 시스템 downloader 파드는 숨긴다(자산 다운로드용, $0/h, 시스템이 자동 복구).
            items = [p for p in items if not _is_system_pod(p)]
        page = paginate([_pod_dict(p) for p in items], limit, cursor)
        page["workspace"] = ALL if len(targets) > 1 else targets[0]
        return page
