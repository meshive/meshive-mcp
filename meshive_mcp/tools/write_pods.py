"""Write tools — pods (write scope).

For create_pod and start_pod (resuming billing of a stopped pod), which cost money, update_pod, which restarts a running
pod and can raise its price, and delete_pod, which can't be undone, **the server (this tool)** enforces the `confirm`
gate: with confirm=false (the default) it returns only an estimate/summary and does nothing. It doesn't rely on the
model's attention.
"""
from __future__ import annotations

import math
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from .. import money
from ..client import meshive_client
from ..serialize import to_dict
from ..workspace import needs_input, resolve, resolve_pod
from ._common import CREATE, DESTRUCTIVE, MUTATE, READ_ONLY, call, meshive_tool, preview, price_change

_POD_ARGS_DOC = """`workspace` takes the id from the workspaces tool (a label also works). `template_id` comes from the templates tool.
GPU pod: pass `gpu_model` (from the gpus tool) and optionally `gpu_count`/`gpu_vram_gb`; vCPU/RAM default to the GPU's recommended sizes.
CPU pod: omit `gpu_model`; `cpu_line` picks the CPU line (a `cpu_line` value from the cpus tool; CPU pods only, default the cheapest line that fits) and vCPU/RAM default to 8 cores / 32 GiB like the console (or the nearest sizes that line offers).
`vcpu` and `ram_gb` are totals for the pod and must be sizes the console offers; a rejected size comes back with the `allowed` values. The system disk is sized by the server (shown as `resources.disk_gb` in the estimate).
`volumes` attaches EXISTING network (nfs) storage from the storages tool: [{"storage": pv_name, "mount_path": "/data"}]. Existing local storage can't be attached to a new pod.
`local_volumes` creates NEW local storage on the pod's machine together with the pod: [{"name": "scratch", "mount_path": "/scratch", "size_gb": 50}] with optional "encrypted", "usage_warning" and "usage_warning_threshold" (0-1). Each needs at least 10 GiB and spot pods can't have one. It is billed hourly by size until it is deleted, also while the pod is stopped and after the pod is deleted unless delete_pod lists it; the estimate includes it as `breakdown.local_storage`.
`ports` opens new ports: a list like [8888, {"port": 6006, "name": "tb", "external": false}]; don't repeat ports or port names the template already has.
`template_ports` changes the ports the template opens, like the console's deploy screen: [{"port": 8188, "name": "ui", "new_port": 8189, "external": false}], where `port` is the template's port and "name" (rename), "new_port" (renumber) and "external" (true = exposed over HTTPS, false = internal only) are optional. Template ports can't be removed; use `ports` to open new ones.
`env` is a dict and `secret_keys` names the secret ones; template variables the template leaves blank need a value. `command` overrides the start command (at most 4096 characters).
`max_price_per_hour` is checked against the estimate (compute plus new local_volumes) before creating and caps the final COMPUTE hourly rate at placement; an over-cap placement fails asynchronously.
Existing storage you attach and Asset Hub retention are billed separately and excluded from this cap. `region` is accepted for compatibility and ignored.
`input_assets` places Asset Hub assets in the pod: ["asset_id" or {"asset": id, "target_dir": "/workspace/models", "role": role, "paths": ["sub/folder/"], "strip_prefix": ""}] (default place /inputs/<asset name> or the template's path for that role); "paths" picks some files or folders, copied from the asset's file list exactly as written there, and "strip_prefix": "" keeps their folder structure (left out, the folder they share is stripped). Only these keys are accepted. They add an input volume billed by size.
`watched_folders` uploads new files in those folders as assets: ["/workspace/results" or {"path": p, "include": ["*.png"], "include_existing": false, "enabled": true}] (the template's own output folders are watched already); `watched_template_folders` changes those template folders from the start, like the console's create screen: {"/outputs": {"enabled": false}} turns one off, {"/outputs": {"include": ["*.png"]}} uploads only matching files. `harvest_destination` {"mode": "user_s3", "credential_id": n} sends them to the user's bucket instead of managed storage.
`model_set` adds one model set to the pod: a `slug` from the model_sets tool for this template (only templates whose list is `selectable`; one set per pod). Its files are added as input assets before the pod is created, so creating takes longer, often several minutes. The estimate shows the set as `model_set` with its file count, size and minimum GPU memory. If a file can't be added, no pod is created (model_set_unavailable) and the files already added are reused next time.
`hf_token_id` is for Quick Deploy templates whose model set is gated on Hugging Face (the model_sets tool shows `hf_token_required` and `gated_sources`): the id of a workspace token from the source_credentials tool whose Hugging Face account accepted the model's license. The set's files download with that token, so the user must first accept the license on the page in `gated_sources` with the same Hugging Face account. Without it the server refuses with hf_token_required; hf_license_not_accepted / hf_access_not_approved mean that account hasn't accepted the license or isn't approved yet, and hf_token_rejected means the token is invalid. Like `model_set`, a confirmed create with it returns only after the files were linked, which can take minutes."""


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


def _bytes_size(value: Any) -> str:
    """Byte count → GiB like the console's other sizes (MiB under 1 GiB)."""
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return "size unknown"
    if not math.isfinite(raw) or raw < 0:
        return "size unknown"
    gib = raw / 1024 ** 3
    if gib >= 10:
        return f"{gib:,.0f} GiB"
    if gib >= 1:
        return f"{gib:.1f} GiB"
    return f"{raw / 1024 ** 2:.0f} MiB"


def _model_set(est: Any) -> dict[str, Any] | None:
    """The model set summary the server adds to a pod estimate when `model_set` was passed."""
    summary = getattr(est, "model_set", None) or (getattr(est, "raw", None) or {}).get("modelSet")
    return summary if isinstance(summary, dict) else None


def _model_set_note(est: Any) -> str:
    """Sentence for the user about the model set in an estimate, or "" when there is none."""
    summary = _model_set(est)
    if summary is None:
        return ""
    note = (f" It adds model set '{summary.get('name') or summary.get('slug')}' ({summary.get('file_count')} files, "
            f"{_bytes_size(summary.get('total_size_bytes'))})")
    need = summary.get("min_vram_gb")
    if isinstance(need, (int, float)) and need > 0:
        note += (f", which needs at least {need:g} GB of GPU memory"
                 + (" (estimated)" if summary.get("min_vram_estimated") else ""))
        have = (est.resources or {}).get("vram_gb")
        if isinstance(have, (int, float)) and have < need:
            note += f", more than the {have:g} GB of the chosen GPU"
    return note + ". Its files are fetched before the pod is created, which can take several minutes."


# update_pod arguments that guard an edit rather than change the pod.
_EDIT_CONTROLS = {"drop_watched_folders", "max_price_per_hour"}

# A create with a model set waits for its files, so the call itself may outlast the client's timeout.
_SLOW_CREATE = (" If the create call times out, don't create the pod again: check operation_status with this "
                "operation_id and the pods tool first.")


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
                           watched_template_folders: dict[str, dict[str, Any]] | None = None,
                           harvest_destination: dict[str, Any] | None = None,
                           template_ports: list[dict[str, Any]] | None = None,
                           model_set: str | None = None, hf_token_id: int | None = None) -> dict[str, Any]:
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
                secret_keys=secret_keys, ports=ports, template_ports=template_ports, command=command,
                internet_premium=internet_premium,
                uptime_premium=uptime_premium, cpu_premium=cpu_premium, max_price_per_hour=max_price_per_hour,
                input_assets=input_assets, watched_folders=watched_folders,
                watched_template_folders=watched_template_folders, harvest_destination=harvest_destination,
                model_set=model_set, hf_token_id=hf_token_id))
        out = to_dict(est)
        out["workspace"] = ws
        if _model_set(est) is not None:
            out["model_set"] = _model_set(est)
        out["next_step"] = ("Show the price per hour and hardware to the user." + _local_storage_note(est) +
                            _model_set_note(est) +
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
                         watched_template_folders: dict[str, dict[str, Any]] | None = None,
                         harvest_destination: dict[str, Any] | None = None,
                         template_ports: list[dict[str, Any]] | None = None,
                         model_set: str | None = None, hf_token_id: int | None = None,
                         confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Create a pod. This spends the user's credit every hour while the pod runs.
        With confirm=false (default) it only returns the estimate and creates nothing; call again with confirm=true
        after the user explicitly agreed to the price. The pod starts asynchronously — poll the pods tool for status.
        With `model_set` or `hf_token_id` the confirmed call returns only after the set's files were added, which can
        take minutes.
        """
        kwargs = _pod_kwargs(gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, rental_type=rental_type,
                             vcpu=vcpu, ram_gb=ram_gb, cpu_line=cpu_line, volumes=volumes, local_volumes=local_volumes,
                             env=env, secret_keys=secret_keys, ports=ports, template_ports=template_ports,
                             command=command,
                             internet_premium=internet_premium, uptime_premium=uptime_premium, cpu_premium=cpu_premium,
                             max_price_per_hour=max_price_per_hour, input_assets=input_assets,
                             watched_folders=watched_folders, watched_template_folders=watched_template_folders,
                             harvest_destination=harvest_destination, model_set=model_set,
                             hf_token_id=hf_token_id)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                est = await call(client.estimate_pod, name, template_id, workspace=ws, **kwargs)
                payload = {"estimate": to_dict(est), "workspace": ws, "name": name}
                if _model_set(est) is not None:
                    payload["model_set"] = _model_set(est)
                return preview("create_pod", payload,
                               f"Create pod '{name}' at {money.hourly(est.price_per_hour)}/hour?" + _local_storage_note(est)
                               + _model_set_note(est)
                               + (_SLOW_CREATE if _model_set(est) is not None or hf_token_id is not None else ""))
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
                        placement: str | None = None, confirm: bool = False, allow_data_loss: bool = False,
                        operation_id: str | None = None) -> dict[str, Any]:
        """Start a stopped pod; hourly billing resumes. With confirm=false (default) it only returns the pod's current
        state and hourly price and changes nothing; call again with confirm=true after the user agreed to pay again.
        `placement`: omit it (default) and the server picks what the console's Start button would; the result's
        `placement` says which one it used. Or choose: "same_node" starts it on its original machine now;
        "wait_same_node" waits until that machine has room and then starts it there; "any_node" moves it to another
        machine now; "wait_any_node" waits until any machine has room and starts it there. A waiting pod shows as
        `waiting` in the pods tool.
        any_node and wait_any_node can move the pod: unpreserved workspace files are then permanently deleted and
        attached local storage stays on the old machine. For those two, set allow_data_loss=true only after separate
        explicit consent to that permanent loss for this pod and placement. allow_data_loss is only sent with an
        explicit placement: when the server's own pick would move a pod with unpreserved files, the call fails with
        data_loss_consent_required and names that placement. Asynchronous — poll pods for `running`."""
        choice = (placement or "").strip().lower() or None
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod)
            current = await call(client.get_pod, pod, ws)
            # Both placements that may land on another machine delete unpreserved workspace files (the server asks the same).
            loss = choice in _MOVING_PLACEMENTS and current.has_unpreserved_workspace is not False
            if not confirm or (loss and not allow_data_loss):
                if choice is None:
                    # The server picks the placement, so whether the pod moves is only known from its answer; a move
                    # that would lose files comes back as data_loss_consent_required with the placement it picked.
                    warning = (" If the server's pick moves the pod to another machine, its unpreserved workspace files "
                               "would be permanently deleted, so the start is refused with data_loss_consent_required "
                               "naming that placement; it then needs separate explicit consent before calling again with "
                               "that placement and allow_data_loss=true."
                               if current.has_unpreserved_workspace is not False else "")
                    where = (" The server picks the placement the console would; it may wait until a machine has room "
                             "and then start by itself."
                             + (" allow_data_loss is not sent without an explicit placement." if allow_data_loss else ""))
                else:
                    warning = (" Unpreserved workspace files will be permanently deleted if the pod moves to another "
                               "node. Obtain separate explicit consent and pass allow_data_loss=true." if loss else "")
                    where = ((" It waits until a machine has room and then starts by itself."
                              if choice in _WAITING_PLACEMENTS else "")
                             + _same_node_note(choice, current.same_node_unavailable_reason))
                return preview("start_pod", {"pod": to_dict(current), "workspace": ws, "placement": choice,
                                             # null: not known until the server picks the placement.
                                             "requires_data_loss_consent": None if choice is None else loss,
                                             "data_loss_warning": warning},
                               f"Start pod '{current.user_alias or pod}' ({current.status})? Billing resumes at "
                               f"{money.hourly(current.price_per_hour)}/hour compute plus "
                               f"{money.hourly(current.storage_rate_per_hour)}/hour storage." + where + warning)
            # Consent is for a known move, so it goes only with an explicit placement.
            result = await call(client.start_pod, pod, ws, placement=choice,
                                allow_data_loss=allow_data_loss if choice is not None else False)
        out = to_dict(result)
        # The placement the server used; older servers don't report it.
        used = getattr(result, "placement", None) or choice
        out["placement"] = used
        step = ""
        if choice is None and used:
            step = f"The server picked placement {used}, the console's default. "
            if used in _MOVING_PLACEMENTS:
                step += ("Tell the user the pod may start on another machine; local storage attached to it stays on "
                         "the old machine. ")
        if used in _WAITING_PLACEMENTS:
            step += ("The start was accepted. The pod is `waiting` until a machine has room, then starts by itself. "
                     "Poll the pods tool for the new status.")
        else:
            step += "The start was accepted and runs asynchronously. Poll the pods tool for the new status."
        out["next_step"] = step
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

    # --- Edit (the console's Pod Edit) ---------------------------------------------------------------------

    @meshive_tool(server, "update_pod", title="Edit Pod", annotations=MUTATE)
    async def update_pod(ctx: Context[Any, Any], pod: str, workspace: str | None = None, name: str | None = None,
                         template_id: int | None = None, rental_type: str | None = None,
                         gpu_model: str | None = None, gpu_vram_gb: int | None = None, gpu_count: int | None = None,
                         vcpu: int | None = None, ram_gb: int | None = None, cpu_line: str | None = None,
                         env: dict[str, str] | None = None, secret_keys: list[str] | None = None,
                         remove_env: list[str] | None = None, ports: list[Any] | None = None,
                         template_ports: list[dict[str, Any]] | None = None, remove_ports: list[int] | None = None,
                         command: str | None = None, volumes: list[dict[str, str]] | None = None,
                         local_volumes: list[dict[str, Any]] | None = None, detach_storages: list[str] | None = None,
                         allow_node_migration: bool = False, allow_data_loss: bool = False,
                         drop_watched_folders: list[str] | None = None, max_price_per_hour: float | None = None,
                         confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Change an existing pod like the console's Edit: pass only what changes. `pod` is the pod_name from the pods tool (its display name also works).
        With confirm=false (default) it only returns the server's estimate (the `changes`, the hourly price before and after, and whether the pod `restarts`) and changes nothing; call again with confirm=true after the user agreed.
        A running pod restarts with the new settings and a stopped pod uses them at its next start; renaming alone (`name`) doesn't restart it. Input assets and watched folders are kept.
        Locked like the console: the pod type (GPU or CPU), premiums and the image can't change.
        Hardware: `gpu_model`/`gpu_vram_gb`/`gpu_count` for GPU pods, `cpu_line` for CPU pods, `rental_type` demand or spot. Changing the GPU resets vCPU and RAM per GPU to the new GPU's recommended sizes, changing only `gpu_count` keeps the per-GPU ratio, and changing the CPU line resets cores and RAM to the console's default. `vcpu` and `ram_gb` are pod totals and must be sizes the console offers. An older pod may need `ram_gb` passed with any change.
        `template_id` moves the pod to another template; its variables, ports and command then start from that template,
        and the preview's changes list what was added before that doesn't carry over.
        `env` sets or adds variables (`secret_keys` names the secret ones) and `remove_env` removes variables the user added. `ports` opens new ports, `template_ports` renames, renumbers or exposes/hides current ports ([{"port": 8188, "name": "ui", "new_port": 8189, "external": false}]) and `remove_ports` closes ports the user added. Template variables and ports can't be removed. `command` replaces the start command ("" = back to the template's).
        Storage: `volumes` attaches existing network storage ([{"storage": pv_name, "mount_path": "/data"}]) or local storage on the pod's current machine; `local_volumes` creates new local storage like create_pod (at least 10 GiB, billed hourly until deleted); `detach_storages` lists pv_names to detach (the storage and its data stay). Spot pods can't have local storage, so detach it to switch to spot.
        If the pod's machine can't fit the change, the call fails with node_migration_required and nothing changes. Only after the user agrees to a move, call again with allow_node_migration=true. A move permanently deletes unpreserved workspace files: when the pod has any, allow_data_loss=true is also needed, only after separate explicit consent (it is only sent together with allow_node_migration).
        If the edit would leave watched folders on no attached storage, it fails with harvest_roots_orphaned and their `paths`; after the user agrees to remove them, pass them as `drop_watched_folders`.
        `max_price_per_hour` refuses the edit when the new estimate is above it."""
        changes = _pod_kwargs(name=name, template_id=template_id, rental_type=rental_type, gpu_model=gpu_model,
                              gpu_vram_gb=gpu_vram_gb, gpu_count=gpu_count, vcpu=vcpu, ram_gb=ram_gb,
                              cpu_line=cpu_line, env=env, secret_keys=secret_keys, remove_env=remove_env, ports=ports,
                              template_ports=template_ports, remove_ports=remove_ports, command=command,
                              volumes=volumes, local_volumes=local_volumes, detach_storages=detach_storages,
                              drop_watched_folders=drop_watched_folders, max_price_per_hour=max_price_per_hour)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod)
            # A move deletes unpreserved workspace files, so it needs the same separate consent as start_pod any_node.
            loss = False
            if allow_node_migration:
                current = await call(client.get_pod, pod, ws)
                loss = current.has_unpreserved_workspace is not False
            if not confirm or (loss and not allow_data_loss):
                # The consent flags don't change the estimate; without them the server prices the edit instead of
                # refusing it for consent, and the consent question is asked here.
                est = await call(client.estimate_pod_update, pod, ws, **changes)
                label = est.name or pod
                question = f"Change pod '{label}': {'; '.join(est.changes) if est.changes else 'no visible change'}?"
                question += price_change(est)
                if set(changes) - _EDIT_CONTROLS == {"name"}:
                    question += " Only the name changes; the pod isn't restarted."
                elif est.restarts:
                    question += " The pod is running, so it restarts with the new settings."
                elif est.restarts is None:
                    question += " A running pod restarts with the new settings; a stopped one uses them at its next start."
                else:
                    question += " The pod isn't running; it uses the new settings at its next start."
                question += _local_storage_note(est)
                warning = ""
                if allow_node_migration:
                    question += " If its machine can't fit the change, the pod moves to another machine."
                    if loss:
                        warning = (" Unpreserved workspace files will be permanently deleted if the pod moves to "
                                   "another machine. Obtain separate explicit consent and pass allow_data_loss=true.")
                elif allow_data_loss:
                    question += " allow_data_loss is only sent together with allow_node_migration."
                return preview("update_pod", {"pod": pod, "workspace": ws, "estimate": to_dict(est),
                                              "allow_node_migration": allow_node_migration,
                                              "requires_data_loss_consent": loss, "data_loss_warning": warning},
                               question + warning)
            result = await call(client.update_pod, pod, ws, **changes, allow_node_migration=allow_node_migration,
                                allow_data_loss=allow_data_loss if allow_node_migration else False)
        out = to_dict(result)
        tx = result.transaction_id
        if result.renamed_only:
            out["next_step"] = "Renamed; the pod wasn't restarted. The pods tool shows the new name."
            return out
        step = "The edit was accepted" + (f" (transaction {tx})" if tx is not None else "") + "."
        restarts = result.estimate.restarts if result.estimate is not None else None
        if restarts:
            step += " The pod restarts with the new settings; poll the pods tool until it is `running` again."
        elif restarts is None:
            step += (" A running pod restarts with the new settings and a stopped one uses them at its next start; "
                     "poll the pods tool for its status.")
        else:
            step += " The pod uses the new settings at its next start (start_pod)."
        if tx is not None:
            step += (f" If it doesn't come back as expected, call the transactions tool with transaction_id={tx}: it "
                     "shows the current step and why it failed.")
        out["next_step"] = step
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
