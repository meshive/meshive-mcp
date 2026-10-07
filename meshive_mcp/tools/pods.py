from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ..workspace import ALL, needs_input, resolve, resolve_many, resolve_pod
from ._common import READ_ONLY, call, meshive_tool


_SECRETS_NOTE = ("Secret connect values are included because show_secrets was set. Give them only to the user who "
                 "asked; never write them into files, commits, logs or other tools.")


def _pod_dict(p: Any, show_secrets: bool = False) -> dict[str, Any]:
    d = to_dict(p)
    d["is_system"] = bool(p.is_downloader)
    if show_secrets:
        # to_dict 가 가린 비밀값을 이 응답에만 다시 채운다.
        d["connect_credentials"] = [{**out, "value": cred.value}
                                    for out, cred in zip(d["connect_credentials"], p.connect_credentials)]
    return d


def register(server: MCPServer) -> None:
    @meshive_tool(server, "pods", title="List Pods", annotations=READ_ONLY)
    async def pods(ctx: Context[Any, Any], workspace: str | None = None, pod: str | None = None,
                   status: str | None = None, include_metrics: bool = False, include_system: bool = False,
                   show_secrets: bool = False, limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the pods (GPU/CPU containers) in a workspace, or show one pod by its `pod_name` (its display name also works).
        `workspace` takes the id from the workspaces tool (a label also works); pass "all" to list pods across every workspace, or omit it if the user has only one.
        Set `include_metrics` for live CPU/RAM/GPU usage of a single pod (`ram_size`, `ephemeral_storage_*` and GPU `vram_size` are MiB); use `status` to filter the list. System-managed downloader pods are hidden unless `include_system` is true; they are free and cannot be stopped or deleted.
        To open a pod, use its `endpoints` (URLs; `readiness` ready/preparing/interrupted) and `connect_credentials` (logins the console shows on Connect, e.g. ComfyUI's ACCESS_PASSWORD). Secret values are null; set `show_secrets` with a single `pod` only when the user asks for the login.
        A stopped pod's `stop_reason_code` (other than "user") says the system stopped it, and `same_node_unavailable_reason` why it cannot start on its machine now."""
        async with meshive_client(ctx) as client:
            if pod is not None:
                ws = await resolve(client, workspace)
                if needs_input(ws):
                    return ws
                pod = await resolve_pod(client, ws, pod)
                item = _pod_dict(await call(client.get_pod, pod, ws), show_secrets)
                if include_metrics:
                    item["metrics"] = to_dict(await call(client.get_pod_metrics, pod, ws))
                out: dict[str, Any] = {"pod": item}
                if show_secrets and any(c["is_secret"] for c in item["connect_credentials"]):
                    out["note"] = _SECRETS_NOTE
                return out
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
            items = [p for p in items if not p.is_downloader]
        page = paginate([_pod_dict(p) for p in items], limit, cursor)
        page["workspace"] = ALL if len(targets) > 1 else targets[0]
        return page
