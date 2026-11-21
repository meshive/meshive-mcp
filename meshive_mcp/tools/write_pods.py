"""Write tools — pods (write scope).

For create_pod and start_pod (resuming billing of a stopped pod), which cost money, update_pod, which restarts a running
pod and can raise its price, and delete_pod, which can't be undone, `confirm` defaults to false: with confirm=false the
tool returns only an estimate/summary and does nothing. A call with confirm=true runs as sent; the server can't tell
whether the user agreed.
"""
from __future__ import annotations

import math
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import money
from ..client import meshive_client
from ..errors import lock_unavailable, lock_unavailable_reason
from ..serialize import to_dict
from ..workspace import needs_input, resolve, resolve_pod, resolve_storage
from . import _ports
from ._common import (CREATE, DESTRUCTIVE, MUTATE, READ_ONLY, REVEAL, bind_pod, call, meshive_tool, preview,
                      price_change)

# What estimate_pod and create_pod say about all their arguments together. The format of each argument is in that
# argument's own description below (the tool's input schema), which keeps the tool descriptions short enough for
# clients that cut long ones.
_POD_ARGS_DOC = """Each argument's format is in its own description. `workspace` and `template_id` come from the workspaces and templates tools.
GPU pod: pass `gpu_model` (from the gpus tool) and optionally `gpu_count`/`gpu_vram_gb`; vCPU/RAM default to the GPU's recommended sizes.
CPU pod: omit `gpu_model`; `cpu_line` picks the CPU line (from the cpus tool; default the cheapest line that fits) and vCPU/RAM default to 8 cores / 32 GiB like the console (or the nearest sizes that line offers).
`vcpu` and `ram_gb` are totals for the pod and must be sizes the console offers. The system disk is sized by the server (shown as `resources.disk_gb` in the estimate).
`volumes` attaches existing network storage. `local_volumes` creates new local storage on the pod's machine, billed hourly by size until it is deleted, also while the pod is stopped and after the pod is deleted unless delete_pod lists it.
`max_price_per_hour` caps the compute hourly rate; existing storage you attach and Asset Hub retention are billed separately.
`input_assets` places Asset Hub assets in the pod. New files in the template's output folders and in `watched_folders` are uploaded as assets; `watched_template_folders` changes the template's folders and `harvest_destination` where the files go.
`model_set` adds one model set to the pod, and `hf_token_id` is the token a set gated on Hugging Face needs; the set's files are added before the pod is created, often several minutes.
Ports exposed over HTTPS are created locked: only workspace members signed in to the console and requests with one of the port's access keys get through, and anything else gets 401. `lock_public_ports=false` (or "locked": false on a port) creates them open to anyone who knows the URL: only when the user explicitly asks for that. One exception: a template port whose template sets it open is created open; the preview lists it as `open_by_template` and says how to lock it."""

_Workspace = Annotated[str | None, Field(description="Workspace id from the workspaces tool (a label also works).")]
_TemplateId = Annotated[int, Field(description="Template id from the templates tool.")]
_GpuModel = Annotated[str | None, Field(description="GPU model from the gpus tool. Omit it for a CPU pod.")]
_CpuLine = Annotated[str | None, Field(description=(
    "CPU pods only: a `cpu_line` value from the cpus tool. Default: the cheapest line that fits."))]
_Vcpu = Annotated[int | None, Field(description="vCPUs for the whole pod, one of the sizes the console offers.")]
_RamGb = Annotated[int | None, Field(description="RAM for the whole pod in GiB, one of the sizes the console offers.")]
_Volumes = Annotated[list[dict[str, str]] | None, Field(description=(
    'Attaches EXISTING network (nfs) storage from the storages tool: [{"storage": pv_name, "mount_path": "/data"}]. '
    "A display name works on estimates/previews; confirm with the returned ids. Existing local storage can't be attached to a new pod."))]
_LocalVolumes = Annotated[list[dict[str, Any]] | None, Field(description=(
    'Creates NEW local storage on the pod\'s machine together with the pod: [{"name": "scratch", "mount_path": '
    '"/scratch", "size_gb": 50}] with optional "encrypted", "usage_warning" and "usage_warning_threshold" (0-1). Each '
    "needs at least 10 GiB and spot pods can't have one. It is billed hourly by size until it is deleted, also while "
    "the pod is stopped and after the pod is deleted unless delete_pod lists it; the estimate includes it as "
    "`breakdown.local_storage`."))]
_Env = Annotated[dict[str, str] | None, Field(description=(
    "Environment variables; template variables the template leaves blank need a value, except a Hugging Face token "
    "(HF_TOKEN): it's only needed for gated or private repos or Hub uploads and is left out when blank, so don't ask "
    "for one to run a public model. `secret_keys` names the secret ones."))]
_SecretKeys = Annotated[list[str] | None, Field(description="Names of the `env` variables that are secret.")]
_Ports = Annotated[list[Any] | None, Field(description=(
    'Opens new ports: a list like [8888, {"port": 6006, "name": "tb", "external": false}]. A port is exposed over '
    'HTTPS unless "external" is false, and an exposed port is locked unless its item says "locked": false (open to '
    "anyone who knows the URL; only when the user explicitly asks). Don't repeat ports or port names the template "
    "already has."))]
_TemplatePorts = Annotated[list[dict[str, Any]] | None, Field(description=(
    'Changes the ports the template opens, like the console\'s deploy screen: [{"port": 8188, "name": "ui", '
    '"new_port": 8189, "external": false}], where `port` is the template\'s port and "name" (rename), "new_port" '
    '(renumber), "external" (true = exposed over HTTPS, false = internal only) and "locked" (for an exposed port: '
    "true = locked, also when the template sets that port open; false = open to anyone who knows the URL, only when "
    "the user explicitly asks) are optional. Template ports can't be removed; use `ports` to open new ones."))]
_LockPublicPorts = Annotated[bool, Field(description=(
    "Leave it true (the default): the ports the pod exposes over HTTPS are created locked, so only workspace members "
    "signed in to the console and requests that carry one of the port's access keys get through. The one exception "
    "is a template port that its template sets open: it stays open unless its `template_ports` item says "
    "\"locked\": true (the preview lists such ports as `open_by_template`). false creates every exposed port open to "
    "anyone who knows the URL: pass it only when the user explicitly asked for open ports. \"locked\" on a port's "
    "item in `ports` or `template_ports` decides for that port alone."))]
_LockNewPublicPorts = Annotated[bool, Field(description=(
    "Leave it true (the default): a port this edit newly exposes over HTTPS is locked (an external port in `ports`, a "
    "port set to \"external\": true), so only workspace members signed in to the console and requests with one of "
    "the port's access keys get through (a new template's ports follow "
    "that template's own setting, and the preview lists the ones it leaves open as `open_by_template`). false leaves "
    "the newly exposed ports open to anyone who knows the URL: pass it only when the user explicitly asked for that. "
    "\"locked\": false on a port's item in `ports` or `template_ports` decides for that port alone, with the same "
    "rule. A port that is exposed already keeps its lock either way."))]
_Command = Annotated[str | None, Field(description="Overrides the start command (at most 32,000 characters).")]
_Region = Annotated[str | None, Field(description="Accepted for compatibility and ignored.")]
_MaxPricePerHour = Annotated[float | None, Field(description=(
    "Checked against the estimate (compute plus new local_volumes) before creating, and caps the final COMPUTE "
    "hourly rate at placement; an over-cap placement fails asynchronously. Existing storage you attach and Asset Hub "
    "retention are billed separately and excluded from this cap."))]
_InputAssets = Annotated[list[Any] | None, Field(description=(
    'Places Asset Hub assets in the pod: ["asset_id" or {"asset": id, "target_dir": "/workspace/models", "role": '
    'role, "paths": ["sub/folder/"], "strip_prefix": ""}] (default place /inputs/<asset name> or the template\'s '
    'path for that role); "paths" picks some files or folders, copied from the asset\'s file list exactly as written '
    'there, and "strip_prefix": "" keeps their folder structure (left out, the folder they share is stripped). Only '
    "these keys are accepted. They add an input volume billed by size."))]
_RemoveInputAssets = Annotated[list[int] | None, Field(description=(
    "Removes input assets: connection_ids of the pod's active input assets from pods with include_assets."))]
_WatchedFolders = Annotated[list[Any] | None, Field(description=(
    'Uploads new files in these folders as assets: ["/workspace/results" or {"path": p, "include": ["*.png"], '
    '"include_existing": false, "enabled": true}] (the template\'s own output folders are watched already). It works '
    "on templates without an output folder too (for example NIM or vLLM), as long as the pod has storage the folders "
    "sit in (`volumes` or `local_volumes`)."))]
_WatchedTemplateFolders = Annotated[dict[str, dict[str, Any]] | None, Field(description=(
    'Changes the template\'s output folders from the start, like the console\'s create screen: {"/outputs": '
    '{"enabled": false}} turns one off, {"/outputs": {"include": ["*.png"]}} uploads only matching files, and '
    '{"/workspace/output": {"path": "/workspace/results"}} moves an output folder (its $MESHIVE_OUTPUT_DIR and '
    "storage follow; output folders only)."))]
_HarvestDestination = Annotated[dict[str, Any] | None, Field(description=(
    '{"mode": "user_s3", "credential_id": n} sends the uploaded files to the user\'s bucket instead of managed '
    "storage. `credential_id` takes a `bucket_id` from buckets."))]
_ModelSet = Annotated[str | None, Field(description=(
    "Adds one model set to the pod: a `slug` from the model_sets tool for this template (only templates whose list "
    "is `selectable`; one set per pod). Its files are added as input assets before the pod is created, so creating "
    "takes longer, often several minutes. The estimate shows the set as `model_set` with its file count, size and "
    "minimum GPU memory. If a file can't be added, no pod is created (model_set_unavailable) and the files already "
    "added are reused next time."))]
_HfTokenId = Annotated[int | None, Field(description=(
    "For Quick Deploy templates whose model set is gated on Hugging Face (the model_sets tool shows "
    "`hf_token_required` and `gated_sources`): the id of a workspace token from the source_credentials tool whose "
    "Hugging Face account accepted the model's license. The set's files download with that token, so the user must "
    "first accept the license on the page in `gated_sources` with the same Hugging Face account. Like `model_set`, a "
    "confirmed create with it returns only after the files were linked, which can take minutes."))]


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
    """Byte count → GiB like the console's other sizes (MiB under 1 GiB, KiB or bytes under 1 MiB, never "0 MiB")."""
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
    if raw >= 1024 ** 2:
        return f"{raw / 1024 ** 2:.0f} MiB"
    if raw >= 1024:
        return f"{round(raw / 1024, 1)} KiB"
    return f"{int(raw)} bytes"


def _model_set(est: Any) -> dict[str, Any] | None:
    """The model set summary the server adds to a pod estimate when `model_set` was passed."""
    summary = getattr(est, "model_set", None) or (getattr(est, "raw", None) or {}).get("modelSet")
    return summary if isinstance(summary, dict) else None


async def _storage_arguments(client: Any, ws: str, args: dict[str, Any], *, confirm: bool = False) -> dict[str, Any]:
    """Resolve existing volumes by name on a preview; confirmed calls keep the returned ids."""
    out = dict(args)
    if out.get("volumes") is not None:
        volumes = []
        for v in out["volumes"]:
            selector = v.get("storage", v.get("pv_name"))
            volume = dict(v)
            if selector is not None:
                volume["storage"] = await resolve_storage(client, ws, selector, confirm=confirm)
                volume.pop("pv_name", None)
            volumes.append(volume)
        out["volumes"] = volumes
    for key in ("detach_storages", "delete_local_storages"):
        if out.get(key) is not None:
            out[key] = [await resolve_storage(client, ws, s, confirm=confirm) for s in out[key]]
    return out


def _storage_label(storage: Any) -> str:
    """'name' (10 GiB) for a pod's mounted volume; capacity is MiB."""
    size = f" ({storage.capacity / 1024:g} GiB)" if getattr(storage, "capacity", 0) else ""
    return f"'{storage.user_alias or storage.pv_name}'{size}"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _deleted_storage_note(pod: Any, delete_local_storages: list[str]) -> tuple[str, str]:
    """For the delete_pod preview: the end of the question (which local volumes go with the pod, and which stay and
    keep being billed) and a next_step hint for the agent when some stay."""
    mounted = {s.pv_name: s for s in getattr(pod, "linked_storages", None) or []}
    gone = [_storage_label(mounted[pv]) if pv in mounted else f"'{pv}'" for pv in delete_local_storages]
    kept = [_storage_label(s) for pv, s in mounted.items()
            if pv not in delete_local_storages and s.storage_type == "hostPath"]
    if len(gone) == 1:
        question = f" and its local volume {gone[0]} with all data on it? This cannot be undone."
    elif gone:
        question = f" and its local volumes {_and(gone)} with all data on them? This cannot be undone."
    else:
        question = "? This cannot be undone."
    if len(kept) == 1:
        question += f" Its local volume {kept[0]} is kept and stays billed until it is deleted."
    elif kept:
        question += f" Its local volumes {_and(kept)} are kept and stay billed until they are deleted."
    hint = (" To delete a kept local volume with the pod, add it to `delete_local_storages` and preview again."
            if kept else "")
    return question, hint


def _storage_preview(kind: str, payload: dict[str, Any], question: str, args: dict[str, Any], *,
                     pod_name: str | None = None) -> dict[str, Any]:
    out = preview(kind, payload, question)
    if pod_name is not None:
        bind_pod(out, pod_name)
    storage_args = {k: args[k] for k in ("volumes", "detach_storages", "delete_local_storages") if args.get(k)}
    if storage_args:
        out["storage_arguments"] = storage_args
        out["next_step"] += (" Use the canonical storage ids in `storage_arguments` for confirmation and every retry; "
                             "display names can change or be reused.")
    return out


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


# Said in an update_pod preview that adds or removes input assets.
_INPUT_ASSETS_NOTE = " Input assets change, so the input volume is sized for the new set and billed by size."
# remove_input_assets drops the row from the pod's configuration; the files of an asset that was added while the pod
# ran can stay in the pod until it is next re-created.
_REMOVE_INPUT_ASSETS_NOTE = (" Removing an asset that was added while the pod was running may leave its files in the "
                             "pod until the pod is next re-created; to delete them now, use remove_pod_asset while "
                             "the pod runs.")
# After the edit: the asset's connection is released, so remove_pod_asset has nothing to act on, and the restart of
# this edit doesn't remove the files either.
_REMOVE_INPUT_ASSETS_DONE_NOTE = (" The files of an asset that was added while the pod was running may stay in the "
                                  "pod until it is re-created again (this edit's restart doesn't remove them).")


# update_pod arguments that guard an edit rather than change the pod.
_EDIT_CONTROLS = {"drop_watched_folders", "max_price_per_hour", "lock_public_ports"}


def _asks_only_for_locks(changes: dict[str, Any]) -> bool:
    """Whether an edit asks for nothing but the lock of ports the pod already exposes: `template_ports` items with
    nothing but "locked". The server applies that without restarting the pod (it refuses it for a port that isn't
    exposed). Whether it did is the server's to say; see _lock_only."""
    if set(changes) - _EDIT_CONTROLS != {"template_ports"}:
        return False
    items = changes["template_ports"]
    return bool(items) and all(isinstance(item, dict) and "locked" in item and set(item) <= {"port", "locked"}
                               for item in items)


def _lock_only(changes: dict[str, Any], restarts: bool | None, transaction_id: Any = None) -> bool:
    """Whether an edit only changed port locks, without a restart. The server's answer decides: an edit it says
    restarts the pod, or runs as a transaction, is an ordinary edit whatever the request looked like. The request's
    shape only settles it when the server doesn't say (`restarts` is null)."""
    return _asks_only_for_locks(changes) and restarts is not True and transaction_id is None


async def _edit(fn: Any, pod: str, ws: str, changes: dict[str, Any], **consent: Any) -> Any:
    """Call estimate_pod_update or update_pod. An edit that only locks ports the pod already exposes has no open-port
    way out when the server can't lock: the ports stay as they are, so its refusal says to try again later instead of
    offering to open or hide a port."""
    try:
        return await call(fn, pod, ws, **changes, **consent)
    except ToolError as exc:
        reason = lock_unavailable_reason(exc)
        if reason is None or not _asks_only_for_locks(changes):
            raise
        raise lock_unavailable(reason, pod_request=False) from exc


def _locks(answer: Any, args: dict[str, Any]) -> _ports.Locks:
    """The locks an estimate or result reports, sorted with the arguments the request was made with."""
    return _ports.read(getattr(answer, "port_locks", None), lock_public_ports=args.get("lock_public_ports", True),
                       ports=args.get("ports"), template_ports=args.get("template_ports"))


# Warning codes in the order a preview lists them, most severe first. The server sends all but
# move_deletes_scratch_disk, which only this server knows to add (the estimate can't tell whether the pod will move).
_WARNING_ORDER = ("deletes_scratch_disk", "move_deletes_scratch_disk", "spot_scratch_erased", "hides_files")
_MAX_WARNINGS = 5


def _clip_path(path: str) -> str:
    """A path as the server's warnings write it: one longer than 28 characters keeps its last 27 after '…'."""
    return path if len(path) <= 28 else "…" + path[-27:]


def _move_message(path: str | None) -> str:
    where = f" at {_clip_path(path)}" if path else ""
    return f"Moving this pod deletes its scratch disk{where} and its files."


def _server_warnings(est: Any) -> list[dict[str, str]]:
    """The short warnings the server attached to an estimate ([] from a server without them)."""
    items = getattr(est, "warnings", None)
    if items is None:
        items = (getattr(est, "raw", None) or {}).get("warnings")
    return [{"code": str(w.get("code") or ""), "message": str(w["message"])}
            for w in items or [] if isinstance(w, dict) and w.get("message")]


def _scratch_paths(pod: Any) -> list[str] | None:
    """The paths of a pod's scratch disks, or None from a server that doesn't list them."""
    disks = getattr(pod, "scratch_disks", None)
    if disks is None:
        disks = (getattr(pod, "raw", None) or {}).get("scratchDisks")
    if not isinstance(disks, list):
        return None
    return [str(d.get("path")) for d in disks if isinstance(d, dict) and d.get("path")]


def _move_warnings(pod: Any, already: list[dict[str, str]]) -> list[dict[str, str]]:
    """For a pod whose move needs data-loss consent: what the move deletes, one entry per scratch disk, leaving out a
    disk that `already` says the edit deletes. Without the paths (an older server) one entry says it without one."""
    deleted = {w["message"] for w in already if w["code"] == "deletes_scratch_disk"}
    paths = _scratch_paths(pod)
    if paths is None:
        return [] if deleted else [{"code": "move_deletes_scratch_disk", "message": _move_message(None)}]
    return [{"code": "move_deletes_scratch_disk", "message": _move_message(path)} for path in paths
            if f"Deletes the scratch disk at {_clip_path(path)} and its files." not in deleted]


def _ordered(warnings: list[dict[str, str]]) -> list[dict[str, str]]:
    """Most severe first; at most _MAX_WARNINGS, dropping hides_files first."""
    rank = {code: i for i, code in enumerate(_WARNING_ORDER)}
    out = sorted(warnings, key=lambda w: rank.get(w["code"], len(rank)))
    while len(out) > _MAX_WARNINGS:
        hidden = [i for i, w in enumerate(out) if w["code"] == "hides_files"]
        out.pop(hidden[-1] if hidden else -1)
    return out


def _with_warnings(out: dict[str, Any], warnings: list[dict[str, str]]) -> dict[str, Any]:
    """Put `warnings` at the top of a response (left out when there are none) and keep it out of the nested
    estimate, so there is one list to show."""
    estimate = out.get("estimate")
    if isinstance(estimate, dict):
        estimate.pop("warnings", None)
    if warnings:
        out["warnings"] = warnings
    else:
        out.pop("warnings", None)
    return out


def _said(warnings: list[dict[str, str]]) -> str:
    """The warnings as sentences for a preview's question."""
    return "".join(f" {w['message']}" for w in warnings)


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
    async def estimate_pod(ctx: Context[Any, Any], name: str, template_id: _TemplateId, workspace: _Workspace = None,
                           gpu_model: _GpuModel = None, gpu_count: int = 1, gpu_vram_gb: int | None = None,
                           rental_type: str = "demand", vcpu: _Vcpu = None, ram_gb: _RamGb = None,
                           cpu_line: _CpuLine = None, volumes: _Volumes = None,
                           local_volumes: _LocalVolumes = None,
                           env: _Env = None, secret_keys: _SecretKeys = None,
                           ports: _Ports = None, command: _Command = None,
                           internet_premium: bool = False, uptime_premium: bool = False, cpu_premium: bool = False,
                           region: _Region = None, max_price_per_hour: _MaxPricePerHour = None,
                           input_assets: _InputAssets = None, watched_folders: _WatchedFolders = None,
                           watched_template_folders: _WatchedTemplateFolders = None,
                           harvest_destination: _HarvestDestination = None,
                           template_ports: _TemplatePorts = None,
                           model_set: _ModelSet = None, hf_token_id: _HfTokenId = None,
                           lock_public_ports: _LockPublicPorts = True) -> dict[str, Any]:
        """Estimate the hourly price of a pod before creating it; nothing is created and no credit is spent.
        Call this first, show the user the price and hardware, then call create_pod with the same arguments.
        `locked_ports` and `open_ports` list the ports the pod would expose over HTTPS, by the lock each would get
        (`open_by_template`: open because the template sets them so, not because of this call).
        """
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            volumes = (await _storage_arguments(client, ws, {"volumes": volumes}))["volumes"]
            est = await call(client.estimate_pod, name, template_id, workspace=ws, **_pod_kwargs(
                gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, rental_type=rental_type, vcpu=vcpu,
                ram_gb=ram_gb, cpu_line=cpu_line, volumes=volumes, local_volumes=local_volumes, env=env,
                secret_keys=secret_keys, ports=ports, template_ports=template_ports, command=command,
                internet_premium=internet_premium,
                uptime_premium=uptime_premium, cpu_premium=cpu_premium, max_price_per_hour=max_price_per_hour,
                input_assets=input_assets, watched_folders=watched_folders,
                watched_template_folders=watched_template_folders, harvest_destination=harvest_destination,
                model_set=model_set, hf_token_id=hf_token_id, lock_public_ports=lock_public_ports))
        out = to_dict(est)
        out["workspace"] = ws
        if _model_set(est) is not None:
            out["model_set"] = _model_set(est)
        warnings = _ordered(_server_warnings(est))
        _with_warnings(out, warnings)
        locks = _locks(est, {"lock_public_ports": lock_public_ports, "ports": ports, "template_ports": template_ports})
        out.update(_ports.summary(locks))
        shown = ("Show the price per hour, hardware and each warnings message" if warnings
                 else "Show the price per hour and hardware")
        out["next_step"] = (shown + " to the user." + _local_storage_note(est) +
                            _model_set_note(est) + _ports.preview_note(locks, creating=True) +
                            " Create the pod only after they agree, by calling create_pod with the same arguments and "
                            "confirm=true.")
        if volumes:
            out["storage_arguments"] = {"volumes": volumes}
            out["next_step"] += (" For create_pod, replace volumes with the canonical ids in `storage_arguments`; "
                                 "keep those ids for every retry.")
        return out

    estimate_pod.__doc__ = (estimate_pod.__doc__ or "") + "\n" + _POD_ARGS_DOC
    meshive_tool(server, "estimate_pod", title="Estimate Pod Price", annotations=READ_ONLY)(estimate_pod)

    async def create_pod(ctx: Context[Any, Any], name: str, template_id: _TemplateId, workspace: _Workspace = None,
                         gpu_model: _GpuModel = None, gpu_count: int = 1, gpu_vram_gb: int | None = None,
                         rental_type: str = "demand", vcpu: _Vcpu = None, ram_gb: _RamGb = None,
                         cpu_line: _CpuLine = None, volumes: _Volumes = None,
                         local_volumes: _LocalVolumes = None,
                         env: _Env = None, secret_keys: _SecretKeys = None,
                         ports: _Ports = None, command: _Command = None,
                         internet_premium: bool = False, uptime_premium: bool = False, cpu_premium: bool = False,
                         region: _Region = None, max_price_per_hour: _MaxPricePerHour = None,
                         input_assets: _InputAssets = None, watched_folders: _WatchedFolders = None,
                         watched_template_folders: _WatchedTemplateFolders = None,
                         harvest_destination: _HarvestDestination = None,
                         template_ports: _TemplatePorts = None,
                         model_set: _ModelSet = None, hf_token_id: _HfTokenId = None,
                         lock_public_ports: _LockPublicPorts = True,
                         confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Create a pod. This spends the user's credit every hour while the pod runs.
        With confirm=false (default) it only returns the estimate and creates nothing; call again with confirm=true
        after the user explicitly agreed to the price. The pod starts asynchronously — poll the pods tool for status.
        With `model_set` or `hf_token_id` the confirmed call returns only after the set's files were added, which can
        take minutes.
        The preview's `locked_ports` and `open_ports` list the ports the pod exposes over HTTPS, by the lock each
        gets; tell the user about any open port before they agree. `open_by_template` are open because the template
        sets them so: ask the user whether to lock them ("locked": true on their `template_ports` items).
        """
        kwargs = _pod_kwargs(gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, rental_type=rental_type,
                             vcpu=vcpu, ram_gb=ram_gb, cpu_line=cpu_line, volumes=volumes, local_volumes=local_volumes,
                             env=env, secret_keys=secret_keys, ports=ports, template_ports=template_ports,
                             command=command,
                             internet_premium=internet_premium, uptime_premium=uptime_premium, cpu_premium=cpu_premium,
                             max_price_per_hour=max_price_per_hour, input_assets=input_assets,
                             watched_folders=watched_folders, watched_template_folders=watched_template_folders,
                             harvest_destination=harvest_destination, model_set=model_set,
                             hf_token_id=hf_token_id, lock_public_ports=lock_public_ports)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            kwargs = await _storage_arguments(client, ws, kwargs, confirm=confirm)
            if not confirm:
                est = await call(client.estimate_pod, name, template_id, workspace=ws, **kwargs)
                locks = _locks(est, kwargs)
                payload = {"estimate": to_dict(est), "workspace": ws, "name": name, **_ports.summary(locks)}
                if _model_set(est) is not None:
                    payload["model_set"] = _model_set(est)
                warnings = _ordered(_server_warnings(est))
                _with_warnings(payload, warnings)
                return _storage_preview("create_pod", payload,
                               f"Create pod '{name}' at {money.hourly(est.price_per_hour)}/hour?" + _local_storage_note(est)
                               + _said(warnings) + _model_set_note(est) + _ports.preview_note(locks, creating=True)
                               + (_SLOW_CREATE if _model_set(est) is not None or hf_token_id is not None else ""), kwargs)
            created = await call(client.create_pod, name, template_id, workspace=ws, **kwargs)
        out = to_dict(created)
        locks = _locks(created, kwargs)
        out.update(_ports.summary(locks))
        tx = created.transaction_id
        in_tx = f" (transaction {tx})" if tx is not None else ""
        if created.pod_name:
            # The server answers pods(pod=<pod_name>) from the first second: creating, then running or why it failed.
            out["next_step"] = (f"Pod '{name}' (pod_name {created.pod_name}) is being created{in_tx}. Poll pods with "
                                f"pod={created.pod_name}: it shows creating until it runs, and why if the create fails.")
        else:
            out["next_step"] = (f"Pod '{name}' is being created{in_tx}. It usually runs within a minute. Poll the pods "
                                "tool (find it by name) for status and the exact billed price; use logs for its output.")
        if tx is not None and not created.pod_name:
            # A failed create drops off the transactions list after a few minutes; the lookup by id has no time window.
            out["next_step"] += (f" If the pod doesn't appear or stays creating, call the transactions tool with "
                                 f"transaction_id={tx}: it shows the current step and why it failed.")
        out["next_step"] += _ports.result_note(locks)
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
        `pod` is the pod_name from the pods tool; a display name works on the preview, and confirm=true needs the
        returned `pod_name`.
        `placement`: omit it (default) and the server picks what the console's Start button would; the result's
        `placement` says which one it used. Or choose: "same_node" starts it on its original machine now;
        "wait_same_node" waits until that machine has room and then starts it there; "any_node" moves it to another
        machine now; "wait_any_node" waits until any machine has room and starts it there. A waiting pod shows as
        `waiting` in the pods tool.
        any_node and wait_any_node can move the pod: any scratch disk it has is then permanently deleted and
        attached local storage stays on the old machine. For those two, set allow_data_loss=true only after separate
        explicit consent to that permanent loss for this pod and placement. allow_data_loss is only sent with an
        explicit placement: when the server's own pick would move a pod with a scratch disk, the call fails with
        data_loss_consent_required and names that placement. Asynchronous — poll pods for `running`."""
        choice = (placement or "").strip().lower() or None
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod, confirm=confirm)
            current = await call(client.get_pod, pod, ws)
            # Both placements that may land on another machine delete the scratch disk (the server asks the same).
            loss = choice in _MOVING_PLACEMENTS and current.has_unpreserved_workspace is not False
            if not confirm or (loss and not allow_data_loss):
                warnings: list[dict[str, str]] = []
                if choice is None:
                    # The server picks the placement, so whether the pod moves is only known from its answer: a move
                    # that deletes a scratch disk comes back as data_loss_consent_required with the placement it picked.
                    where = (" The server picks the placement the console would; it may wait until a machine has room "
                             "and then start by itself."
                             + (" allow_data_loss is not sent without an explicit placement." if allow_data_loss else ""))
                else:
                    if loss:
                        # A pod whose only scratch disk is an old one waiting for cleanup lists none, but the
                        # move still needs consent: say so without a path.
                        warnings = _ordered(_move_warnings(current, [])) or [
                            {"code": "move_deletes_scratch_disk", "message": _move_message(None)}]
                    where = ((" It waits until a machine has room and then starts by itself."
                              if choice in _WAITING_PLACEMENTS else "")
                             + _same_node_note(choice, current.same_node_unavailable_reason))
                consent = " Obtain separate explicit consent and pass allow_data_loss=true." if loss else ""
                return bind_pod(_with_warnings(preview("start_pod", {
                    "pod": to_dict(current), "workspace": ws, "placement": choice,
                    # null: not known until the server picks the placement.
                    "requires_data_loss_consent": None if choice is None else loss,
                    "data_loss_warning": _said(warnings).strip()},
                    f"Start pod '{current.user_alias or pod}' ({current.status})? Billing resumes "
                    f"at {money.hourly(current.price_per_hour)}/hour compute plus "
                    f"{money.hourly(current.storage_rate_per_hour)}/hour storage." + where + _said(warnings) + consent),
                    warnings), pod)
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
        """Restart a pod in place (same machine, same storage). Asynchronous — poll pods for `running`.
        A 401 from a port whose `access` is locked (pods tool) is the lock working; a restart doesn't change it."""
        return await _lifecycle(ctx, "restart_pod", pod, workspace, "restart")

    @meshive_tool(server, "delete_pod", title="Delete Pod", annotations=DESTRUCTIVE)
    async def delete_pod(ctx: Context[Any, Any], pod: str, workspace: str | None = None,
                         delete_local_storages: list[str] | None = None, confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Delete a pod permanently. With confirm=false (default) it only returns what would be deleted; call again
        with confirm=true after the user agreed. Attached network storage survives; local (hostPath) volumes are deleted
        only if listed in `delete_local_storages` by pv_name or display name on the preview. Confirm with the returned
        `pod_name` and storage ids: a display name is refused with confirm=true."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod, confirm=confirm)
            delete_local_storages = (await _storage_arguments(client, ws, {"delete_local_storages": delete_local_storages},
                                                             confirm=confirm))["delete_local_storages"]
            if not confirm:
                current = await call(client.get_pod, pod, ws)
                question, hint = _deleted_storage_note(current, delete_local_storages or [])
                out = _storage_preview("delete_pod", {"pod": to_dict(current), "workspace": ws,
                                                      "delete_local_storages": delete_local_storages or []},
                                       f"Delete pod '{current.user_alias or pod}' ({current.status})" + question,
                                       {"delete_local_storages": delete_local_storages}, pod_name=pod)
                out["next_step"] += hint
                return out
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
                         enable_folder_saving: bool = False, lock_public_ports: _LockNewPublicPorts = True,
                         add_input_assets: _InputAssets = None, remove_input_assets: _RemoveInputAssets = None,
                         confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Change an existing pod like the console's Edit: pass only what changes. `pod` is the pod_name from the pods tool (its display name works on the preview; confirm with the returned `pod_name`).
        With confirm=false (default) it only returns the server's estimate (the `changes`, the hourly price before and after, and whether the pod `restarts`) and changes nothing; call again with confirm=true after the user agreed.
        A running pod restarts with the new settings and a stopped pod uses them at its next start; renaming alone (`name`) doesn't restart it. Watched folders are kept.
        Locked like the console: the pod type (GPU or CPU), premiums and the image can't change.
        Hardware: `gpu_model`/`gpu_vram_gb`/`gpu_count` for GPU pods, `cpu_line` for CPU pods, `rental_type` demand or spot. Changing the GPU resets vCPU and RAM per GPU to the new GPU's recommended sizes, changing only `gpu_count` keeps the per-GPU ratio, and changing the CPU line resets cores and RAM to the console's default. `vcpu` and `ram_gb` are pod totals and must be sizes the console offers. An older pod may need `ram_gb` passed with any change.
        `template_id` moves the pod to another template; its variables, ports and command then start from that template,
        and the preview's changes list what was added before that doesn't carry over.
        `env` sets or adds variables (`secret_keys` names the secret ones) and `remove_env` removes variables the user added. `ports` opens new ports, `template_ports` renames, renumbers or exposes/hides current ports ([{"port": 8188, "name": "ui", "new_port": 8189, "external": false}]) and `remove_ports` closes ports the user added. Template variables and ports can't be removed. `command` replaces the start command ("" = back to the template's).
        Port locks: a port this edit newly exposes over HTTPS is locked (see `lock_public_ports`); a new template's ports follow that template (`open_by_template` in the preview). A port that is exposed already keeps its lock; set_port_lock changes it without a restart. The preview lists `locked_ports` and `open_ports`.
        Storage: `volumes` attaches existing network storage ([{"storage": pv_name, "mount_path": "/data"}]) or local storage on the pod's current machine; `local_volumes` creates new local storage like create_pod (at least 10 GiB, billed hourly until deleted); `detach_storages` lists pv_names to detach (the storage and its data stay). Names work on previews; confirm with the returned ids. Spot pods can't have local storage, so detach it to switch to spot.
        If the pod's machine can't fit the change, the call fails with node_migration_required and nothing changes. Only after the user agrees to a move, call again with allow_node_migration=true. A move permanently deletes any scratch disk the pod has; then allow_data_loss=true is also needed, only after separate explicit consent (it is only sent together with allow_node_migration).
        If the edit would leave watched folders on no attached storage, it fails with harvest_roots_orphaned and their `paths`; after the user agrees to remove them, pass them as `drop_watched_folders`.
        `enable_folder_saving=true` adds the uploader to an older pod that has none (watched_folders shows editable false with reason no_sidecar), so its folders can be watched; it needs storage on the pod and restarts a running pod once.
        `max_price_per_hour` refuses the edit when the new estimate is above it.
        `add_input_assets` and `remove_input_assets` (connection_ids from pods include_assets) change the pod's input assets with a restart; add_pod_asset adds one without."""
        changes = _pod_kwargs(name=name, template_id=template_id, rental_type=rental_type, gpu_model=gpu_model,
                              gpu_vram_gb=gpu_vram_gb, gpu_count=gpu_count, vcpu=vcpu, ram_gb=ram_gb,
                              cpu_line=cpu_line, env=env, secret_keys=secret_keys, remove_env=remove_env, ports=ports,
                              template_ports=template_ports, remove_ports=remove_ports, command=command,
                              volumes=volumes, local_volumes=local_volumes, detach_storages=detach_storages,
                              drop_watched_folders=drop_watched_folders, max_price_per_hour=max_price_per_hour,
                              enable_folder_saving=True if enable_folder_saving else None,
                              add_input_assets=add_input_assets, remove_input_assets=remove_input_assets,
                              lock_public_ports=lock_public_ports)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod, confirm=confirm)
            changes = await _storage_arguments(client, ws, changes, confirm=confirm)
            # A move deletes the scratch disk, so it needs the same separate consent as start_pod any_node.
            loss = False
            current = None
            if allow_node_migration:
                current = await call(client.get_pod, pod, ws)
                loss = current.has_unpreserved_workspace is not False
            if not confirm or (loss and not allow_data_loss):
                # The consent flags don't change the estimate; without them the server prices the edit instead of
                # refusing it for consent, and the consent question is asked here.
                est = await _edit(client.estimate_pod_update, pod, ws, changes)
                warnings = _server_warnings(est)
                moving: list[dict[str, str]] = []
                if allow_node_migration and loss:
                    moving = _move_warnings(current, warnings)
                    # Every scratch disk is already deleted by the edit itself (deletes_scratch_disk), or the pod
                    # has none it keeps: the move deletes nothing more, so there is nothing to consent to.
                    if not moving and _scratch_paths(current) is not None:
                        loss = False
            if not confirm or (loss and not allow_data_loss):
                label = est.name or pod
                question = f"Change pod '{label}': {'; '.join(est.changes) if est.changes else 'no visible change'}?"
                question += price_change(est)
                locks = _locks(est, changes)
                if set(changes) - _EDIT_CONTROLS == {"name"}:
                    question += " Only the name changes; the pod isn't restarted."
                elif _lock_only(changes, est.restarts):
                    question += " Only the port lock changes; it applies within seconds and the pod isn't restarted."
                elif est.restarts:
                    question += " The pod is running, so it restarts with the new settings."
                elif est.restarts is None:
                    question += " A running pod restarts with the new settings; a stopped one uses them at its next start."
                else:
                    question += " The pod isn't running; it uses the new settings at its next start."
                question += _local_storage_note(est) + _ports.preview_note(locks, creating=False)
                if add_input_assets or remove_input_assets:
                    question += _INPUT_ASSETS_NOTE
                if remove_input_assets:
                    question += _REMOVE_INPUT_ASSETS_NOTE
                if allow_node_migration:
                    question += " If its machine can't fit the change, the pod moves to another machine."
                elif allow_data_loss:
                    question += " allow_data_loss is only sent together with allow_node_migration."
                warnings = _ordered(warnings + moving)
                consent = " Obtain separate explicit consent and pass allow_data_loss=true." if loss else ""
                moved = [w for w in warnings if w["code"] == "move_deletes_scratch_disk"]
                return _storage_preview("update_pod", _with_warnings({
                    "pod": pod, "workspace": ws, "estimate": to_dict(est), **_ports.summary(locks),
                    "allow_node_migration": allow_node_migration, "requires_data_loss_consent": loss,
                    "data_loss_warning": _said(moved).strip()}, warnings),
                    question + _said(warnings) + consent, changes, pod_name=pod)
            result = await _edit(client.update_pod, pod, ws, changes, allow_node_migration=allow_node_migration,
                                 allow_data_loss=allow_data_loss if allow_node_migration else False)
        out = to_dict(result)
        locks = _locks(result, changes)
        out.update(_ports.summary(locks))
        tx = result.transaction_id
        if result.renamed_only:
            out["next_step"] = "Renamed; the pod wasn't restarted. The pods tool shows the new name."
            return out
        restarts = result.estimate.restarts if result.estimate is not None else None
        if _lock_only(changes, restarts, tx):
            out["next_step"] = ("The port lock was changed without restarting the pod; it applies within seconds."
                                + _ports.result_note(locks))
            return out
        step = "The edit was accepted" + (f" (transaction {tx})" if tx is not None else "") + "."
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
        if remove_input_assets:
            step += _REMOVE_INPUT_ASSETS_DONE_NOTE
        out["next_step"] = step + _ports.result_note(locks)
        return out

    # --- Watched folders (harvest folders of a running Pod) ---------------------------------------------------

    @meshive_tool(server, "watched_folders", title="Show Watched Folders", annotations=READ_ONLY)
    async def watched_folders(ctx: Context[Any, Any], pod: str, workspace: str | None = None) -> dict[str, Any]:
        """Show a pod's watched folders: folders whose new files are uploaded as Asset Hub assets.
        Each folder has `origin` (template or user), `enabled`, its include patterns and `blocked_reason` (network_storage = on network storage, nested_volume = overlaps your volume; never watched).
        `revision` is what set_watched_folders needs as `expected_version`; `editable` false (with a reason) means it can't be changed now.
        Reason no_sidecar means the pod has no uploader: when `can_enable_saving` is true, update_pod with enable_folder_saving=true adds it (one restart); otherwise the pod first needs storage (update_pod `volumes`/`local_volumes`)."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod)
            result = await call(client.get_watched_folders, pod, ws)
        out = to_dict(result)
        out["revision"] = result.revision
        return out

    @meshive_tool(server, "set_watched_folders", title="Set Watched Folders", annotations=MUTATE,
                  conditional_confirm=True)
    async def set_watched_folders(ctx: Context[Any, Any], pod: str, expected_version: int, workspace: str | None = None,
                                  template: dict[str, Any] | None = None, user: list[Any] | None = None,
                                  confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Replace a running pod's watched folders without restarting it. Read watched_folders first and send the WHOLE set back:
        `template` = {template folder path: {"enabled": bool, "include": [...] or null}} and `user` = [{"path", "include", "enabled", "include_existing"}].
        `expected_version` is the `revision` you read; a version conflict means someone else changed it — read again.
        A running pod's template folder can't be moved: to save another folder instead, turn the template folder off and add the new path to `user` (the app keeps writing to $MESHIVE_OUTPUT_DIR, so tell the user to point the app there).
        Adding or turning on a folder can raise storage costs (uploaded files are stored and billed): with confirm=false (default) such a change only returns a summary; call again with confirm=true and the returned `pod_name` after the user agreed. Removing or turning off applies immediately."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            pod = await resolve_pod(client, ws, pod, confirm=confirm)
            if not confirm:
                current = await call(client.get_watched_folders, pod, ws)
                before = {f.path: f.enabled for f in current.folders or []}
                wanted = {path: bool((t or {}).get("enabled", True)) for path, t in (template or {}).items()}
                for item in user or []:
                    path = item if isinstance(item, str) else item.get("path")
                    wanted[path] = True if isinstance(item, str) else bool(item.get("enabled", True))
                grows = [path for path, on in wanted.items() if on and not before.get(path, False)]
                if grows:
                    current_pod = await call(client.get_pod, pod, ws)
                    return bind_pod(preview("set_watched_folders", {"pod": pod, "workspace": ws, "adds_or_enables": grows,
                                                                    "current": to_dict(current)},
                                            f"Watch {', '.join(grows)} on '{current_pod.user_alias or pod}'? New files there are uploaded as assets "
                                            "and their storage is billed."), pod)
            result = await call(client.set_watched_folders, pod, ws, expected_version=expected_version,
                                template=template, user=user)
        out = to_dict(result)
        out["revision"] = result.revision
        out["next_step"] = ("Saved; the pod picks it up without a restart. `applied` turns true once its harvester has "
                            "it" + (" — this pod needs a restart to apply it." if result.restart_hint else "."))
        return out

    # --- SSH (G04) ---------------------------------------------------------------------------------

    @meshive_tool(server, "ssh_access", title="Get SSH Access", annotations=REVEAL, operation_record=False)
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
