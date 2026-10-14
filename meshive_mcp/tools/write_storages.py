"""Write tools — storage (write scope)."""
from __future__ import annotations

import math
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from .. import money
from ..client import meshive_client
from ..serialize import to_dict
from ..workspace import needs_input, resolve
from ._common import CREATE, DESTRUCTIVE, call, meshive_tool, preview


def _volume_size(mib: Any) -> str:
    """Volume size (MiB) → the same notation as the CLI and console: GiB, or MiB under 1 GiB (a 1 GiB encrypted volume minus the LUKS header ≈ 1008 MiB).
    Putting GB on a MiB number turns a 100 GiB volume into '102400.0 GB'."""
    try:
        raw = float(mib)
    except (TypeError, ValueError):
        return "size unknown"
    if not math.isfinite(raw):
        return "size unknown"
    if 0 < raw < 1024:
        return f"{raw:.0f} MiB"
    gib = raw / 1024
    return f"{gib:,.0f} GiB" if gib >= 10 or gib == int(gib) else f"{gib:.1f} GiB"


def register(server: MCPServer) -> None:
    @meshive_tool(server, "create_storage", title="Create Storage", annotations=CREATE)
    async def create_storage(ctx: Context[Any, Any], name: str, size_gb: int, workspace: str | None = None,
                             storage_type: str = "nfs", disk_type: str = "NVMe", encrypted: bool = False,
                             region: str | None = None, max_price_per_hour: float | None = None,
                             usage_warning: bool = False, usage_warning_threshold: float | None = None,
                             confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Create a network (nfs) storage volume of `size_gb` GiB (at least 10, like the console) that any pod in the
        workspace can attach. It is billed hourly by capacity for as long as it exists, mounted or not.
        With confirm=false (default) it only returns the estimate; call again with confirm=true after the user agreed.
        `encrypted` turns on at-rest encryption. `usage_warning` warns the user when the volume fills past
        `usage_warning_threshold` (0-1, default 0.9 = 90% full), like the console's usage warning.
        Local storage can't be created on its own: create it together with a pod through create_pod `local_volumes`.
        `storage_type` stays "nfs" ("hostPath" is refused); `disk_type` and `region` are accepted for compatibility
        and ignored.
        The volume appears in the storages tool shortly after; attach it to a pod with create_pod `volumes`."""
        # disk_type and region are ignored by the server, so they are accepted here and not sent.
        kwargs: dict[str, Any] = dict(storage_type=storage_type, encrypted=encrypted,
                                      max_price_per_hour=max_price_per_hour)
        # The usage warning is sent only when asked for, so the server defaults (off, 0.9) apply otherwise.
        if usage_warning:
            kwargs["usage_warning"] = True
        if usage_warning_threshold is not None:
            kwargs["usage_warning_threshold"] = usage_warning_threshold
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                est = await call(client.estimate_storage, name, size_gb, workspace=ws, **kwargs)
                threshold = usage_warning_threshold if usage_warning_threshold is not None else 0.9
                return preview("create_storage", {"estimate": to_dict(est), "workspace": ws, "name": name},
                               f"Create {size_gb} GiB network ({est.storage_type}) storage '{name}' at "
                               f"{money.hourly(est.price_per_hour)}/hour?"
                               + (f" A usage warning is sent when it is {threshold:.0%} full." if usage_warning else ""))
            created = await call(client.create_storage, name, size_gb, workspace=ws, **kwargs)
        out = to_dict(created)
        out["next_step"] = (f"Storage '{name}' is being created (transaction {created.transaction_id}). Find its pv_name in "
                            "the storages tool once it is `running`.")
        return out

    @meshive_tool(server, "delete_storage", title="Delete Storage", annotations=DESTRUCTIVE)
    async def delete_storage(ctx: Context[Any, Any], storage: str, workspace: str | None = None,
                             confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Delete a storage volume and all data on it. `storage` is the pv_name from the storages tool.
        With confirm=false (default) it only returns what would be deleted (including pods that still mount it);
        call again with confirm=true after the user agreed. Volumes mounted by a user pod are refused (storage_in_use)."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                current = await call(client.get_storage, storage, ws)
                return preview("delete_storage", {"storage": to_dict(current), "workspace": ws},
                               f"Delete storage '{current.user_alias or storage}' ({_volume_size(current.total_size)}, "
                               f"{len(current.linked_pods)} linked pod(s))? All data on it is lost.")
            result = await call(client.delete_storage, storage, ws)
        out = to_dict(result)
        out["next_step"] = "Deletion was accepted. The volume disappears from the storages list within a minute."
        return out
