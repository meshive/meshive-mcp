"""쓰기 도구 — 파드 (write 스코프). 계약 §2.1.

돈이 드는 create_pod·start_pod(정지 파드 과금 재개) 와 되돌릴 수 없는 delete_pod 는 `confirm` 게이트를 **서버(이 도구)가**
강제한다: confirm=false(기본) 면 견적/요약만 돌려주고 아무것도 하지 않는다. 모델의 주의력에 기대지 않는다.
"""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from .. import money
from ..client import meshive_client
from ..serialize import to_dict
from ..workspace import needs_input, resolve, resolve_pod
from ._common import CREATE, DESTRUCTIVE, MUTATE, READ_ONLY, call, meshive_tool, preview

_POD_ARGS_DOC = """`workspace` takes the id from the workspaces tool (a label also works). `template_id` comes from the templates tool.
For a GPU pod pass `gpu_model` (from the gpus tool) and optionally `gpu_count`/`gpu_vram_gb`; omit `gpu_model` for a CPU pod.
vCPU/RAM/disk default to the recommended sizes; `volumes` attaches existing storage as [{"storage": pv_name, "mount_path": "/data"}];
`ports` is a list like [8888, {"port": 6006, "name": "tb", "external": false}]; `env` is a dict and `secret_keys` names the secret ones.
`max_price_per_hour` caps the final COMPUTE hourly rate; an over-cap placement fails asynchronously.
Attached/automatic storage and Asset Hub retention are billed separately and excluded from this cap."""


def _pod_kwargs(**kw: Any) -> dict[str, Any]:
    return {k: v for k, v in kw.items() if v is not None}


def register(server: MCPServer) -> None:
    async def estimate_pod(ctx: Context[Any, Any], name: str, template_id: int, workspace: str | None = None,
                           gpu_model: str | None = None, gpu_count: int = 1, gpu_vram_gb: int | None = None,
                           rental_type: str = "demand", vcpu: int | None = None, ram_gb: int | None = None,
                           disk_gb: int | None = None, volumes: list[dict[str, str]] | None = None,
                           env: dict[str, str] | None = None, secret_keys: list[str] | None = None,
                           ports: list[Any] | None = None, command: str | None = None,
                           internet_premium: bool = False, uptime_premium: bool = False, cpu_premium: bool = False,
                           region: str | None = None, max_price_per_hour: float | None = None) -> dict[str, Any]:
        """Estimate the hourly price of a pod before creating it; nothing is created and no credit is spent.
        Call this first, show the user the price and hardware, then call create_pod with the same arguments.
        """
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            est = await call(client.estimate_pod, name, template_id, workspace=ws, **_pod_kwargs(
                gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, rental_type=rental_type, vcpu=vcpu,
                ram_gb=ram_gb, disk_gb=disk_gb, volumes=volumes, env=env, secret_keys=secret_keys, ports=ports,
                command=command, internet_premium=internet_premium, uptime_premium=uptime_premium,
                cpu_premium=cpu_premium, region=region, max_price_per_hour=max_price_per_hour))
        out = to_dict(est)
        out["workspace"] = ws
        out["next_step"] = ("Show the price per hour and hardware to the user. Create the pod only after they agree, "
                            "by calling create_pod with the same arguments and confirm=true.")
        return out

    estimate_pod.__doc__ = (estimate_pod.__doc__ or "") + "\n" + _POD_ARGS_DOC
    meshive_tool(server, "estimate_pod", annotations=READ_ONLY)(estimate_pod)

    async def create_pod(ctx: Context[Any, Any], name: str, template_id: int, workspace: str | None = None,
                         gpu_model: str | None = None, gpu_count: int = 1, gpu_vram_gb: int | None = None,
                         rental_type: str = "demand", vcpu: int | None = None, ram_gb: int | None = None,
                         disk_gb: int | None = None, volumes: list[dict[str, str]] | None = None,
                         env: dict[str, str] | None = None, secret_keys: list[str] | None = None,
                         ports: list[Any] | None = None, command: str | None = None,
                         internet_premium: bool = False, uptime_premium: bool = False, cpu_premium: bool = False,
                         region: str | None = None, max_price_per_hour: float | None = None,
                         confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Create a pod. This spends the user's credit every hour while the pod runs.
        With confirm=false (default) it only returns the estimate and creates nothing; call again with confirm=true
        after the user explicitly agreed to the price. The pod starts asynchronously — poll the pods tool for status.
        """
        kwargs = _pod_kwargs(gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, rental_type=rental_type,
                             vcpu=vcpu, ram_gb=ram_gb, disk_gb=disk_gb, volumes=volumes, env=env, secret_keys=secret_keys,
                             ports=ports, command=command, internet_premium=internet_premium,
                             uptime_premium=uptime_premium, cpu_premium=cpu_premium, region=region,
                             max_price_per_hour=max_price_per_hour)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                est = await call(client.estimate_pod, name, template_id, workspace=ws, **kwargs)
                return preview("create_pod", {"estimate": to_dict(est), "workspace": ws, "name": name},
                               f"Create pod '{name}' at {money.hourly(est.price_per_hour)}/hour?")
            created = await call(client.create_pod, name, template_id, workspace=ws, **kwargs)
        out = to_dict(created)
        out["next_step"] = (f"Pod '{name}' is being created (transaction {created.transaction_id}). It usually runs within a "
                            "minute. Poll the pods tool (find it by name) for status and the exact billed price; use logs "
                            "for its output.")
        return out

    create_pod.__doc__ = (create_pod.__doc__ or "") + "\n" + _POD_ARGS_DOC
    meshive_tool(server, "create_pod", annotations=CREATE)(create_pod)

    async def _lifecycle(ctx, method: str, pod: str, workspace: str | None, action: str, **extra: Any) -> dict[str, Any]:
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod)
            result = await call(getattr(client, method), pod, ws, **extra)
        out = to_dict(result)
        out["next_step"] = f"The {action} was accepted and runs asynchronously. Poll the pods tool for the new status."
        return out

    @meshive_tool(server, "stop_pod", annotations=MUTATE)
    async def stop_pod(ctx: Context[Any, Any], pod: str, workspace: str | None = None, operation_id: str | None = None) -> dict[str, Any]:
        """Stop a running pod (scale to zero). Pod billing stops; attached storage keeps being billed.
        `pod` is the pod_name from the pods tool (the display name also works). The change is asynchronous — poll pods for `stopped`.
        Use delete_pod to remove the pod entirely."""
        return await _lifecycle(ctx, "stop_pod", pod, workspace, "stop")

    @meshive_tool(server, "start_pod", annotations=MUTATE)
    async def start_pod(ctx: Context[Any, Any], pod: str, workspace: str | None = None,
                        placement: str = "same_node", confirm: bool = False, allow_data_loss: bool = False,
                        operation_id: str | None = None) -> dict[str, Any]:
        """Start a stopped pod; hourly billing resumes. With confirm=false (default) it only returns the pod's current
        state and hourly price and changes nothing; call again with confirm=true after the user agreed to pay again.
        `placement` "same_node" (default) restarts on the original machine and may wait if it is busy; "any_node" moves
        to another machine. Unpreserved workspace files are permanently deleted after migration; attached local
        hostPath storage stays on the old machine. Set allow_data_loss=true only after separate explicit consent
        to that permanent loss for this pod and placement. Asynchronous — poll pods for `running`."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod)
            current = await call(client.get_pod, pod, ws)
            loss = placement == "any_node" and current.has_unpreserved_workspace is not False
            if not confirm or (loss and not allow_data_loss):
                warning = (" Unpreserved workspace files will be permanently deleted if the pod moves to another node. "
                           "Obtain separate explicit consent and pass allow_data_loss=true." if loss else "")
                return preview("start_pod", {"pod": to_dict(current), "workspace": ws, "placement": placement,
                                             "requires_data_loss_consent": loss, "data_loss_warning": warning},
                               f"Start pod '{current.user_alias or pod}' ({current.status})? Billing resumes at "
                               f"{money.hourly(current.price_per_hour)}/hour compute plus "
                               f"{money.hourly(current.storage_rate_per_hour)}/hour storage." + warning)
            result = await call(client.start_pod, pod, ws, placement=placement, allow_data_loss=allow_data_loss)
        out = to_dict(result)
        out["next_step"] = "The start was accepted and runs asynchronously. Poll the pods tool for the new status."
        return out

    @meshive_tool(server, "restart_pod", annotations=MUTATE)
    async def restart_pod(ctx: Context[Any, Any], pod: str, workspace: str | None = None, operation_id: str | None = None) -> dict[str, Any]:
        """Restart a pod in place (same machine, same storage). Asynchronous — poll pods for `running`."""
        return await _lifecycle(ctx, "restart_pod", pod, workspace, "restart")

    @meshive_tool(server, "delete_pod", annotations=DESTRUCTIVE)
    async def delete_pod(ctx: Context[Any, Any], pod: str, workspace: str | None = None,
                         delete_local_storages: list[str] | None = None, confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Delete a pod permanently. With confirm=false (default) it only returns what would be deleted; call again
        with confirm=true after the user agreed. Attached network storage survives; local (hostPath) volumes are deleted
        only if listed in `delete_local_storages` by pv_name."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod)
            if not confirm:
                current = await call(client.get_pod, pod, ws)
                return preview("delete_pod", {"pod": to_dict(current), "workspace": ws,
                                              "delete_local_storages": delete_local_storages or []},
                               f"Delete pod '{current.user_alias or pod}' ({current.status})? This cannot be undone.")
            result = await call(client.delete_pod, pod, ws, delete_local_storages=delete_local_storages)
        out = to_dict(result)
        out["next_step"] = "Deletion was accepted. The pod disappears from the pods list within a minute."
        return out
