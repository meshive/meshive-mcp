"""Write tools — storage (write scope)."""
from __future__ import annotations

import json
import math
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .. import money
from ..client import meshive_client
from ..errors import invalid_argument
from ..serialize import to_dict
from ..workspace import needs_input, resolve
from ._common import CREATE, DESTRUCTIVE, MUTATE, call, meshive_tool, preview, price_change

# How long connect_storage waits for the storage to be ready. Hosted MCP requests sit behind a proxy that ends calls
# after about 100 seconds, so a call never waits longer than this and tells the agent to call again instead.
_CONNECT_WAIT_DEFAULT = 60.0
_CONNECT_WAIT_MAX = 90.0

_SSH_NOTE = ("This password opens a shell with access to the files on this storage. Give it only to the user who "
             "asked; never run the command yourself or put the password in files, commits or other tools. It expires in "
             "a few minutes and a new confirmed call issues a new one.")


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

    @meshive_tool(server, "update_storage", title="Edit Storage", annotations=MUTATE)
    async def update_storage(ctx: Context[Any, Any], storage: str, workspace: str | None = None,
                             name: str | None = None, size_gb: int | None = None, usage_warning: bool | None = None,
                             usage_warning_threshold: float | None = None, confirm: bool = False,
                             operation_id: str | None = None) -> dict[str, Any]:
        """Change a storage volume like the console's Edit: rename it (`name`), grow it (`size_gb`, in GiB) or set its
        usage warning (`usage_warning` on/off, `usage_warning_threshold` 0-1). Pass only what changes; `storage` is the
        pv_name from the storages tool.
        Storage can grow but never shrink (storage_cannot_shrink), and growing raises its hourly price, which is billed
        for as long as the volume exists.
        With confirm=false (default) it only returns the estimate (new and current size and hourly price) and changes
        nothing; call again with confirm=true after the user agreed. Growing runs asynchronously; renaming and the
        usage warning apply right away."""
        kwargs = {k: v for k, v in dict(name=name, size_gb=size_gb, usage_warning=usage_warning,
                                        usage_warning_threshold=usage_warning_threshold).items() if v is not None}
        if not kwargs:
            raise invalid_argument("Pass at least one of `name`, `size_gb`, `usage_warning` or `usage_warning_threshold`.")
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                est = await call(client.estimate_storage_update, storage, ws, **kwargs)
                parts = []
                if name is not None:
                    parts.append(f"rename it to '{est.name or name}'")
                if size_gb is not None:
                    new_size = est.size_gb if est.size_gb is not None else size_gb
                    if new_size == est.current_size_gb:
                        parts.append(f"keep it at {new_size} GiB")
                    else:
                        parts.append((f"grow it from {est.current_size_gb} GiB to {new_size} GiB"
                                      if est.current_size_gb is not None else f"grow it to {new_size} GiB")
                                     + " (it can't shrink again)")
                if usage_warning is not None or usage_warning_threshold is not None:
                    on = est.usage_warning if est.usage_warning is not None else usage_warning is not False
                    threshold = next(t for t in (est.usage_warning_threshold, usage_warning_threshold, 0.9)
                                     if t is not None)
                    parts.append(f"warn when it is {threshold:.0%} full" if on else "turn its usage warning off")
                label = storage if name is not None else est.name or storage
                return preview("update_storage", {"estimate": to_dict(est), "workspace": ws, "storage": storage},
                               f"Update storage '{label}': {', '.join(parts)}?" + price_change(est))
            result = await call(client.update_storage, storage, ws, **kwargs)
        out = to_dict(result)
        tx = result.transaction_id
        out["next_step"] = (f"Growing the volume runs asynchronously (transaction {tx}); the storages tool shows the new "
                            "size when it is done." if tx is not None else "Saved; the change applies right away.")
        return out

    @meshive_tool(server, "connect_storage", title="Connect to Storage", annotations=MUTATE)
    async def connect_storage(ctx: Context[Any, Any], storage: str, workspace: str | None = None,
                              wait: float = _CONNECT_WAIT_DEFAULT, confirm: bool = False,
                              operation_id: str | None = None) -> dict[str, Any]:
        """Get an SSH login to a storage volume's files, like the console's Connect, only when the user asks to connect: the `command` to run and a one-time `password` that expires in a few minutes (`expires_at`).
        `storage` is the pv_name from the storages tool. Encrypted storage is unlocked and an access pod is started for it; it locks again by itself after `auto_lock_minutes` (`remaining_seconds` left). Other storage is reached through the access pod that already runs for it.
        With confirm=false (default) it only returns the storage's connection state and changes nothing; call again with confirm=true after the user agreed. It needs a read & write key.
        The confirmed call waits up to `wait` seconds (0-90, default 60) for the storage to be ready; if it isn't yet, the result has no password and says to call again.
        Give the command and password to the user and nobody else: never run the command yourself or put the password in files, commits or other tools. Each confirmed call issues a new password, and it isn't kept as an operation record."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                current = await call(client.get_storage, storage, ws)
                state = await call(client.get_storage_connection, storage, ws)
                label = current.user_alias or storage
                if state.encrypted:
                    question = (f"Unlock encrypted storage '{label}' and start an access pod for SSH? It locks again "
                                "by itself" + (f" after {state.auto_lock_minutes} minutes." if state.auto_lock_minutes
                                               else " after a while."))
                else:
                    question = f"Open an SSH login to the files on storage '{label}'?"
                return preview("connect_storage", {"storage": storage, "workspace": ws, "connection": to_dict(state)},
                               question + " The answer includes a one-time SSH password, which is a secret for you "
                                          "alone.")
            seconds = min(max(float(wait), 0.0), _CONNECT_WAIT_MAX)
            try:
                conn = await call(client.connect_storage, storage, ws,
                                  **({"wait": True, "timeout": seconds} if seconds > 0 else {"wait": False}))
            except ToolError as exc:
                # The server keeps preparing the storage after the SDK stops waiting; report where it is.
                if json.loads(str(exc)).get("code") != "wait_timeout":
                    raise
                conn = await call(client.get_storage_connection, storage, ws)
        out = to_dict(conn)
        ssh = getattr(conn, "ssh", None)
        phase = (conn.phase or "").lower()
        if ssh is not None and phase == "ready":
            # web_url carries the password in the URL — don't leave one more copy in the conversation history.
            out.update(command=ssh.command, password=ssh.password,
                       expires_at=ssh.expires_at.isoformat() if ssh.expires_at else None, note=_SSH_NOTE)
            out["next_step"] = ("Give the command and password to the user." +
                                (f" The storage locks again by itself after {conn.auto_lock_minutes} minutes."
                                 if conn.encrypted and conn.auto_lock_minutes else ""))
        elif phase == "error":
            out["next_step"] = ("Preparing the storage for SSH failed. Tell the user; they can try again later or check "
                                "the storage in the console. Don't retry on your own.")
        else:
            out["next_step"] = (f"The storage is still being prepared ({conn.phase}). Call connect_storage again with "
                                "confirm=true in about 30 seconds to get the SSH login.")
        return out
