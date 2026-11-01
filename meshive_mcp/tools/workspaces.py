from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..serialize import to_dict
from ._common import CREATE, READ_ONLY, call, meshive_tool, preview


def register(server: MCPServer) -> None:
    @meshive_tool(server, "workspaces", title="List Workspaces", annotations=READ_ONLY)
    async def workspaces(ctx: Context[Any, Any], workspace: str | None = None) -> dict[str, Any]:
        """List the user's workspaces, or show one workspace in detail with its cost summary and members (its `ram` and `total_storage` are MiB).
        Omit `workspace` to list; pass a workspace id (the `namespace_name` from the list) for details.
        Other tools take the same `workspace` id, so use this to find it when the user names a workspace by label.
        A new workspace's `status` is `creating` until it is `running`; wait for `running` before creating anything in it.
        Use create_workspace when the user has none or wants another."""
        async with meshive_client(ctx) as client:
            if workspace is None:
                items = await call(client.list_workspaces)
                out: dict[str, Any] = {"items": [to_dict(w) for w in items], "total": len(items)}
                if not items:
                    out["next_step"] = ("The user has no workspace. Offer to create one with the create_workspace tool "
                                        "(it costs nothing by itself), or they can add one in the console. If one was "
                                        "just created, check again before creating another: it can take a moment to "
                                        "appear here.")
                return out
            detail = await call(client.get_workspace, workspace)
            members = await call(client.list_members, workspace)
        return {"workspace": to_dict(detail), "members": [to_dict(m) for m in members]}

    @meshive_tool(server, "create_workspace", title="Create Workspace", annotations=CREATE)
    async def create_workspace(ctx: Context[Any, Any], name: str | None = None, confirm: bool = False,
                               operation_id: str | None = None) -> dict[str, Any]:
        """Create a workspace, the place pods, storage, tasks and servings live in. An empty workspace costs nothing;
        what runs in it is billed as usual. `name` is optional (at most 63 characters) and must differ from the names of
        all the user's workspaces; omit it to get a generated name like `my-workspace-ab12`.
        With confirm=false (default) it only checks the name and creates nothing; call again with confirm=true after the
        user agreed. The workspace is set up in the background and its `status` stays `creating` until it is `running`:
        poll the workspaces tool, and create nothing in it before then."""
        name = (name or "").strip() or None
        async with meshive_client(ctx) as client:
            if not confirm:
                est = await call(client.estimate_workspace, name)
                label = est.name or name
                if label:
                    question = f"Create the workspace '{label}'?"
                else:
                    question = ("Create a workspace with a generated name (like `my-workspace-ab12`)? Ask the user "
                                "for a name first if they care what it is called.")
                question += (" A workspace costs nothing by itself; the pods, storage and tasks the user runs in it "
                             "are billed as usual.")
                return preview("create_workspace", {"estimate": to_dict(est), "name": label}, question)
            created = await call(client.create_workspace, name)
        out = to_dict(created)
        out["next_step"] = (f"Workspace '{created.name}' (id `{created.workspace}`) is being set up. Call the "
                            "`workspaces` tool with no arguments until its status is `running` before creating "
                            "anything in it: pods and tasks are refused until then. `error`, or a `terminated` that "
                            "doesn't change, means setup failed: tell the user. If it is still `creating` after a few "
                            "minutes, tell the user it is taking longer than usual and don't create another. It can "
                            "also be missing from the list for a moment: keep polling.")
        return out
