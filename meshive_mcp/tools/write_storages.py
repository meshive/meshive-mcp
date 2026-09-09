"""쓰기 도구 — 스토리지 (write 스코프). 계약 §2.2."""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from .. import money
from ..client import meshive_client
from ..serialize import to_dict
from ..workspace import needs_input, resolve
from ._common import CREATE, DESTRUCTIVE, call, meshive_tool, preview


def register(server: MCPServer) -> None:
    @meshive_tool(server, "create_storage", annotations=CREATE)
    async def create_storage(ctx: Context[Any, Any], name: str, size_gb: int, workspace: str | None = None,
                             storage_type: str = "nfs", disk_type: str = "NVMe", encrypted: bool = False,
                             region: str | None = None, max_price_per_hour: float | None = None,
                             confirm: bool = False) -> dict[str, Any]:
        """Create a storage volume. It is billed hourly by capacity for as long as it exists, mounted or not.
        With confirm=false (default) it only returns the estimate; call again with confirm=true after the user agreed.
        `storage_type` "nfs" (network, attachable to any pod; supports `encrypted` at-rest encryption) or "hostPath" (local,
        faster, single machine; cannot be encrypted).
        The volume appears in the storages tool shortly after; attach it to a pod with create_pod `volumes`."""
        kwargs = dict(storage_type=storage_type, disk_type=disk_type, encrypted=encrypted, region=region,
                      max_price_per_hour=max_price_per_hour)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                est = await call(client.estimate_storage, name, size_gb, workspace=ws, **kwargs)
                return preview("create_storage", {"estimate": to_dict(est), "workspace": ws, "name": name},
                               f"Create {size_gb} GB {est.storage_type} storage '{name}' at "
                               f"{money.hourly(est.price_per_hour)}/hour?")
            created = await call(client.create_storage, name, size_gb, workspace=ws, **kwargs)
        out = to_dict(created)
        out["next_step"] = (f"Storage '{name}' is being created (transaction {created.transaction_id}). Find its pv_name in "
                            "the storages tool once it is `running`.")
        return out

    @meshive_tool(server, "delete_storage", annotations=DESTRUCTIVE)
    async def delete_storage(ctx: Context[Any, Any], storage: str, workspace: str | None = None,
                             confirm: bool = False) -> dict[str, Any]:
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
                               f"Delete storage '{current.user_alias or storage}' ({current.total_size} GB, "
                               f"{len(current.linked_pods)} linked pod(s))? All data on it is lost.")
            result = await call(client.delete_storage, storage, ws)
        out = to_dict(result)
        out["next_step"] = "Deletion was accepted. The volume disappears from the storages list within a minute."
        return out
