"""Write tools — pods (write scope).

For create_pod and start_pod (resuming billing of a stopped pod), which cost money, and delete_pod, which can't be undone, **the server (this tool)**
enforces the `confirm` gate: with confirm=false (the default) it returns only an estimate/summary and does nothing. It doesn't rely on the model's attention.
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
GPU pod: pass `gpu_model` (from the gpus tool) and optionally `gpu_count`/`gpu_vram_gb`; vCPU/RAM default to the GPU's recommended sizes.
CPU pod: omit `gpu_model`; `cpu_line` picks the CPU line (a `cpu_line` value from the cpus tool; CPU pods only, default the cheapest line that fits) and vCPU/RAM default to 8 cores / 32 GiB like the console (or the nearest sizes that line offers).
`vcpu` and `ram_gb` are totals for the pod and must be sizes the console offers; a rejected size comes back with the `allowed` values. The system disk is sized by the server (shown as `resources.disk_gb` in the estimate).
`volumes` attaches EXISTING network (nfs) storage from the storages tool: [{"storage": pv_name, "mount_path": "/data"}]. Existing local storage can't be attached to a new pod.
`local_volumes` creates NEW local storage on the pod's machine together with the pod: [{"name": "scratch", "mount_path": "/scratch", "size_gb": 50}] with optional "encrypted", "usage_warning" and "usage_warning_threshold" (0-1). Each needs at least 10 GiB and spot pods can't have one. It is billed hourly by size until it is deleted, also while the pod is stopped and after the pod is deleted unless delete_pod lists it; the estimate includes it as `breakdown.local_storage`.
`ports` is a list like [8888, {"port": 6006, "name": "tb", "external": false}]; don't repeat ports or port names the template already has. `env` is a dict and `secret_keys` names the secret ones; template variables the template leaves blank need a value. `command` overrides the start command (at most 4096 characters).
`max_price_per_hour` is checked against the estimate (compute plus new local_volumes) before creating and caps the final COMPUTE hourly rate at placement; an over-cap placement fails asynchronously.
Existing storage you attach and Asset Hub retention are billed separately and excluded from this cap. `region` is accepted for compatibility and ignored.
`input_assets` places Asset Hub assets in the pod: ["asset_id" or {"asset": id, "target_dir": "/workspace/models", "role": role, "paths": ["sub/folder/"]}] (default place /inputs/<asset name> or the template's path for that role); they add an input volume billed by size.
`watched_folders` uploads new files in those folders as assets: ["/workspace/results" or {"path": p, "include": ["*.png"], "include_existing": false}] (the template's own output folders are watched already); `harvest_destination` {"mode": "user_s3", "credential_id": n} sends them to the user's bucket instead of managed storage."""


def _pod_kwargs(**kw: Any) -> dict[str, Any]:
    """SDK keyword arguments for estimate_pod/create_pod. None is left out so the SDK and server defaults apply.
    `region` is not among them: the server ignores it, so the tools accept it and don't send it."""
    return {k: v for k, v in kw.items() if v is not None}


# Breakdown line the server adds when the request creates local_volumes (hourly, included in the total).
_LOCAL_STORAGE = "local_storage"


def _local_storage_note(est: Any) -> str:
    """Sentence for the user about the new local storage priced into an estimate, or "" when there is none."""
    rate = (est.breakdown or {}).get(_LOCAL_STORAGE)
    if rate is None:
        return ""
    return (f" This includes {money.hourly(rate)}/hour for the new local storage, which is billed until that storage "
            "is deleted, also while the pod is stopped.")


# start_pod placements that may land on another machine — both need separate data-loss consent.
_MOVING_PLACEMENTS = ("any_node", "wait_any_node")
# Placements that start or wait on the pod's original machine.
_SAME_NODE_PLACEMENTS = ("same_node", "wait_same_node")
_WAITING_PLACEMENTS = ("wait_same_node", "wait_any_node")


def _same_node_note(placement: str, reason: str | None) -> str:
    """Why a same-machine start can't happen right now, from the pod's `same_node_unavailable_reason`."""
    if placement not in _SAME_NODE_PLACEMENTS or not reason:
        return ""
    if reason == "spec_mismatch":
        # The saved spec differs from the original machine, so the server refuses both same-machine placements;
        # waiting for that machine would never finish.
        return (" Its saved hardware no longer matches its original machine, so same_node and wait_same_node are "
                "refused and waiting won't help; use placement any_node or wait_any_node.")
    reason = reason.replace("_", " ")
    if placement == "wait_same_node":
        return (f" Its original machine can't take it now ({reason}); wait_same_node keeps it waiting until that "
                "changes, any_node or wait_any_node move it instead.")
    return (f" It cannot start on its original machine now ({reason}); wait_same_node waits until that changes, "
            "any_node or wait_any_node move it.")


def register(server: MCPServer) -> None:
    async def estimate_pod(ctx: Context[Any, Any], name: str, template_id: int, workspace: str | None = None,
                           gpu_model: str | None = None, gpu_count: int = 1, gpu_vram_gb: int | None = None,
                           rental_type: str = "demand", vcpu: int | None = None, ram_gb: int | None = None,
                           cpu_line: str | None = None, volumes: list[dict[str, str]] | None = None,
                           local_volumes: list[dict[str, Any]] | None = None,
                           env: dict[str, str] | None = None, secret_keys: list[str] | None = None,
                           ports: list[Any] | None = None, command: str | None = None,
                           internet_premium: bool = False, uptime_premium: bool = False, cpu_premium: bool = False,
                           region: str | None = None, max_price_per_hour: float | None = None,
                           input_assets: list[Any] | None = None, watched_folders: list[Any] | None = None,
                           harvest_destination: dict[str, Any] | None = None) -> dict[str, Any]:
        """Estimate the hourly price of a pod before creating it; nothing is created and no credit is spent.
        Call this first, show the user the price and hardware, then call create_pod with the same arguments.
        """
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            est = await call(client.estimate_pod, name, template_id, workspace=ws, **_pod_kwargs(
                gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, rental_type=rental_type, vcpu=vcpu,
                ram_gb=ram_gb, cpu_line=cpu_line, volumes=volumes, local_volumes=local_volumes, env=env,
                secret_keys=secret_keys, ports=ports, command=command, internet_premium=internet_premium,
                uptime_premium=uptime_premium, cpu_premium=cpu_premium, max_price_per_hour=max_price_per_hour,
                input_assets=input_assets, watched_folders=watched_folders, harvest_destination=harvest_destination))
        out = to_dict(est)
        out["workspace"] = ws
        out["next_step"] = ("Show the price per hour and hardware to the user." + _local_storage_note(est) +
                            " Create the pod only after they agree, by calling create_pod with the same arguments and "
                            "confirm=true.")
        return out

    estimate_pod.__doc__ = (estimate_pod.__doc__ or "") + "\n" + _POD_ARGS_DOC
    meshive_tool(server, "estimate_pod", title="Estimate Pod Price", annotations=READ_ONLY)(estimate_pod)

    async def create_pod(ctx: Context[Any, Any], name: str, template_id: int, workspace: str | None = None,
                         gpu_model: str | None = None, gpu_count: int = 1, gpu_vram_gb: int | None = None,
                         rental_type: str = "demand", vcpu: int | None = None, ram_gb: int | None = None,
                         cpu_line: str | None = None, volumes: list[dict[str, str]] | None = None,
                         local_volumes: list[dict[str, Any]] | None = None,
                         env: dict[str, str] | None = None, secret_keys: list[str] | None = None,
                         ports: list[Any] | None = None, command: str | None = None,
                         internet_premium: bool = False, uptime_premium: bool = False, cpu_premium: bool = False,
                         region: str | None = None, max_price_per_hour: float | None = None,
                         input_assets: list[Any] | None = None, watched_folders: list[Any] | None = None,
                         harvest_destination: dict[str, Any] | None = None,
                         confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Create a pod. This spends the user's credit every hour while the pod runs.
        With confirm=false (default) it only returns the estimate and creates nothing; call again with confirm=true
        after the user explicitly agreed to the price. The pod starts asynchronously — poll the pods tool for status.
        """
        kwargs = _pod_kwargs(gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, rental_type=rental_type,
                             vcpu=vcpu, ram_gb=ram_gb, cpu_line=cpu_line, volumes=volumes, local_volumes=local_volumes,
                             env=env, secret_keys=secret_keys, ports=ports, command=command,
                             internet_premium=internet_premium, uptime_premium=uptime_premium, cpu_premium=cpu_premium,
                             max_price_per_hour=max_price_per_hour, input_assets=input_assets,
                             watched_folders=watched_folders, harvest_destination=harvest_destination)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                est = await call(client.estimate_pod, name, template_id, workspace=ws, **kwargs)
                return preview("create_pod", {"estimate": to_dict(est), "workspace": ws, "name": name},
                               f"Create pod '{name}' at {money.hourly(est.price_per_hour)}/hour?" + _local_storage_note(est))
            created = await call(client.create_pod, name, template_id, workspace=ws, **kwargs)
        out = to_dict(created)
        tx = created.transaction_id
        out["next_step"] = (f"Pod '{name}' is being created" + (f" (transaction {tx})" if tx is not None else "") +
                            ". It usually runs within a minute. Poll the pods tool (find it by name) for status and the "
                            "exact billed price; use logs for its output.")
        if tx is not None:
            # A failed create drops off the transactions list after a few minutes; the lookup by id has no time window.
            out["next_step"] += (f" If the pod doesn't appear or stays creating, call the transactions tool with "
                                 f"transaction_id={tx}: it shows the current step and why it failed.")
        return out

    create_pod.__doc__ = (create_pod.__doc__ or "") + "\n" + _POD_ARGS_DOC
    meshive_tool(server, "create_pod", title="Create Pod", annotations=CREATE)(create_pod)

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

    @meshive_tool(server, "stop_pod", title="Stop Pod", annotations=MUTATE)
    async def stop_pod(ctx: Context[Any, Any], pod: str, workspace: str | None = None, operation_id: str | None = None) -> dict[str, Any]:
        """Stop a running pod (scale to zero). Pod billing stops; attached storage keeps being billed.
        `pod` is the pod_name from the pods tool (the display name also works). The change is asynchronous — poll pods for `stopped`.
        Use delete_pod to remove the pod entirely."""
        return await _lifecycle(ctx, "stop_pod", pod, workspace, "stop")

    @meshive_tool(server, "start_pod", title="Start Pod", annotations=MUTATE)
    async def start_pod(ctx: Context[Any, Any], pod: str, workspace: str | None = None,
                        placement: str = "same_node", confirm: bool = False, allow_data_loss: bool = False,
                        operation_id: str | None = None) -> dict[str, Any]:
        """Start a stopped pod; hourly billing resumes. With confirm=false (default) it only returns the pod's current
        state and hourly price and changes nothing; call again with confirm=true after the user agreed to pay again.
        `placement`: "same_node" (default) starts it on its original machine now; "wait_same_node" waits until that
        machine has room and then starts it there; "any_node" moves it to another machine now; "wait_any_node" waits
        until any machine has room and starts it there. A waiting pod shows as `waiting` in the pods tool.
        any_node and wait_any_node can move the pod: unpreserved workspace files are then permanently deleted and
        attached local storage stays on the old machine. For those two, set allow_data_loss=true only after separate
        explicit consent to that permanent loss for this pod and placement. Asynchronous — poll pods for `running`."""
        choice = (placement or "").strip().lower()
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod)
            current = await call(client.get_pod, pod, ws)
            # Both placements that may land on another machine delete unpreserved workspace files (the server asks the same).
            loss = choice in _MOVING_PLACEMENTS and current.has_unpreserved_workspace is not False
            if not confirm or (loss and not allow_data_loss):
                warning = (" Unpreserved workspace files will be permanently deleted if the pod moves to another node. "
                           "Obtain separate explicit consent and pass allow_data_loss=true." if loss else "")
                return preview("start_pod", {"pod": to_dict(current), "workspace": ws, "placement": choice,
                                             "requires_data_loss_consent": loss, "data_loss_warning": warning},
                               f"Start pod '{current.user_alias or pod}' ({current.status})? Billing resumes at "
                               f"{money.hourly(current.price_per_hour)}/hour compute plus "
                               f"{money.hourly(current.storage_rate_per_hour)}/hour storage."
                               + (" It waits until a machine has room and then starts by itself."
                                  if choice in _WAITING_PLACEMENTS else "")
                               + _same_node_note(choice, current.same_node_unavailable_reason) + warning)
            result = await call(client.start_pod, pod, ws, placement=choice, allow_data_loss=allow_data_loss)
        out = to_dict(result)
        if choice in _WAITING_PLACEMENTS:
            out["next_step"] = ("The start was accepted. The pod is `waiting` until a machine has room, then starts by "
                                "itself. Poll the pods tool for the new status.")
        else:
            out["next_step"] = "The start was accepted and runs asynchronously. Poll the pods tool for the new status."
        return out

    @meshive_tool(server, "restart_pod", title="Restart Pod", annotations=MUTATE)
    async def restart_pod(ctx: Context[Any, Any], pod: str, workspace: str | None = None, operation_id: str | None = None) -> dict[str, Any]:
        """Restart a pod in place (same machine, same storage). Asynchronous — poll pods for `running`."""
        return await _lifecycle(ctx, "restart_pod", pod, workspace, "restart")

    @meshive_tool(server, "delete_pod", title="Delete Pod", annotations=DESTRUCTIVE)
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

    # --- Watched folders (harvest folders of a running Pod) ---------------------------------------------------

    @meshive_tool(server, "watched_folders", title="Show Watched Folders", annotations=READ_ONLY)
    async def watched_folders(ctx: Context[Any, Any], pod: str, workspace: str | None = None) -> dict[str, Any]:
        """Show a pod's watched folders: folders whose new files are uploaded as Asset Hub assets.
        Each folder has `origin` (template or user), `enabled`, its include patterns and `blocked_reason` (network_storage = on network storage, never watched).
        `revision` is what set_watched_folders needs as `expected_version`; `editable` false (with a reason) means it can't be changed now."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod)
            result = await call(client.get_watched_folders, pod, ws)
        out = to_dict(result)
        out["revision"] = result.revision
        return out

    @meshive_tool(server, "set_watched_folders", title="Set Watched Folders", annotations=MUTATE)
    async def set_watched_folders(ctx: Context[Any, Any], pod: str, expected_version: int, workspace: str | None = None,
                                  template: dict[str, Any] | None = None, user: list[Any] | None = None,
                                  confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Replace a running pod's watched folders without restarting it. Read watched_folders first and send the WHOLE set back:
        `template` = {template folder path: {"enabled": bool, "include": [...] or null}} and `user` = [{"path", "include", "enabled", "include_existing"}].
        `expected_version` is the `revision` you read; a version conflict means someone else changed it — read again.
        Adding or turning on a folder can raise storage costs (uploaded files are stored and billed): with confirm=false (default) such a change only returns a summary; call again with confirm=true after the user agreed. Removing or turning off applies immediately."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod)
            if not confirm:
                current = await call(client.get_watched_folders, pod, ws)
                before = {f.path: f.enabled for f in current.folders or []}
                wanted = {path: bool((t or {}).get("enabled", True)) for path, t in (template or {}).items()}
                for item in user or []:
                    path = item if isinstance(item, str) else item.get("path")
                    wanted[path] = True if isinstance(item, str) else bool(item.get("enabled", True))
                grows = [path for path, on in wanted.items() if on and not before.get(path, False)]
                if grows:
                    return preview("set_watched_folders", {"pod": pod, "workspace": ws, "adds_or_enables": grows,
                                                           "current": to_dict(current)},
                                   f"Watch {', '.join(grows)} on {pod}? New files there are uploaded as assets "
                                   "and their storage is billed.")
            result = await call(client.set_watched_folders, pod, ws, expected_version=expected_version,
                                template=template, user=user)
        out = to_dict(result)
        out["revision"] = result.revision
        out["next_step"] = ("Saved; the pod picks it up without a restart. `applied` turns true once its harvester has "
                            "it" + (" — this pod needs a restart to apply it." if result.restart_hint else "."))
        return out

    # --- SSH (G04) ---------------------------------------------------------------------------------

    @meshive_tool(server, "ssh_access", title="Get SSH Access", annotations=MUTATE)
    async def ssh_access(ctx: Context[Any, Any], pod: str, workspace: str | None = None) -> dict[str, Any]:
        """Get a one-time SSH login for a pod, only when the user asks to connect: the `command` to run and a `password` that expires in a few minutes (`expires_at`).
        It needs a read & write key; each call issues a new password. Give both to the user and nothing else — never run the command yourself or put the password in files, commits or other tools."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod)
            access = await call(client.ssh_access, pod, ws)
        # web_url carries the password in the URL — don't leave one more copy in the conversation history.
        return {"pod": pod, "workspace": ws, "command": access.command, "password": access.password,
                "expires_at": access.expires_at.isoformat() if access.expires_at else None,
                "note": ("This password opens a shell in the pod. Give it only to the user who asked; it expires in a few "
                         "minutes and a new call issues a new one.")}
