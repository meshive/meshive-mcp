from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..errors import NEXT_STEP_WORKSPACE_NOT_EMPTY, invalid_argument, tool_error
from ..serialize import to_dict
from ..workspace import resolve_target
from ._common import CREATE, DESTRUCTIVE, MUTATE, READ_ONLY, call, meshive_tool, preview

_CONFIRM_BY_ID = ("Show this to the user and ask for an explicit go-ahead. Only then call this tool again with `workspace` "
                  "set to the id `{ws}`, the other arguments unchanged and confirm=true. Never set confirm=true on your own.")


def _preview_by_id(kind: str, ws: str, payload: dict[str, Any], question: str) -> dict[str, Any]:
    """`preview` for a tool that takes a label on the preview but only the id with confirm=true (see resolve_target), so
    "the same arguments" would send the agent into a refusal."""
    return {**preview(kind, payload, question), "next_step": _CONFIRM_BY_ID.format(ws=ws)}


def register(server: MCPServer) -> None:
    @meshive_tool(server, "workspaces", title="List Workspaces", annotations=READ_ONLY)
    async def workspaces(ctx: Context[Any, Any], workspace: str | None = None) -> dict[str, Any]:
        """List the user's workspaces, or show one workspace in detail with its cost summary and members (its `ram` and `total_storage` are MiB).
        Omit `workspace` to list; pass a workspace id (the `namespace_name` from the list) for details.
        Other tools take the same `workspace` id, so use this to find it when the user names a workspace by label.
        A new workspace's `status` is `creating` until it is `running`; wait for `running` before creating anything in it.
        Use create_workspace when the user has none or wants another, update_workspace to rename one and delete_workspace
        to delete an empty one."""
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

    @meshive_tool(server, "update_workspace", title="Rename Workspace", annotations=MUTATE)
    async def update_workspace(ctx: Context[Any, Any], workspace: str, name: str, confirm: bool = False,
                               operation_id: str | None = None) -> dict[str, Any]:
        """Rename a workspace (it needs the admin or billing role). Only the name changes: the id stays, so whatever uses
        the id keeps working, and every member sees the new name.
        `workspace` is required and never picked for you: pass its id, or its label for the preview.
        `name` is at most 63 characters and must differ from the names of the user's other workspaces (name_taken).
        With confirm=false (default) it only checks the name and changes nothing; call again with the workspace id and
        confirm=true after the user agreed (a label is refused there). The rename applies at once, but the workspaces
        list can take a moment to show the new name, so keep using the id."""
        name = (name or "").strip()
        if not name:
            raise invalid_argument("`name` is required — pass the workspace's new name.")
        async with meshive_client(ctx) as client:
            ws = await resolve_target(client, workspace, confirm=confirm)
            if not confirm:
                est = await call(client.estimate_workspace_update, ws, name=name)
                return _preview_by_id("update_workspace", ws, to_dict(est),
                                      f"Rename the workspace '{est.current_name}' to '{est.name}'? Its id stays `{ws}`. "
                                      "Other members see the new name too.")
            updated = await call(client.update_workspace, ws, name=name)
        out = to_dict(updated)
        out["next_step"] = (f"The workspace is now named '{updated.name}'; its id is still `{updated.workspace}`. Keep using "
                            "the id: the workspaces list can take a moment to show the new name.")
        return out

    @meshive_tool(server, "delete_workspace", title="Delete Workspace", annotations=DESTRUCTIVE)
    async def delete_workspace(ctx: Context[Any, Any], workspace: str, confirm: bool = False,
                               operation_id: str | None = None) -> dict[str, Any]:
        """Delete an empty workspace (it needs the admin or billing role). This can't be undone.
        `workspace` is required and never picked for you: pass its id, or its label for the preview.
        A workspace that still holds anything is refused (workspace_not_empty) and `blockers` counts what is left: pods,
        storage, servings, tasks (finished ones too), task outputs still being saved, assets, custom templates and models,
        credentials, webhooks, inference keys, other members, open invitations, pending payouts and operations in
        progress. Follow its next_step, and clear nothing the user didn't ask you to.
        With confirm=false (default) it only checks and deletes nothing; call again with the workspace id and confirm=true
        after the user agreed (a label is refused there). Deletion runs in the background: the workspace leaves the list
        within a moment, and its name stays taken for a while."""
        async with meshive_client(ctx) as client:
            ws = await resolve_target(client, workspace, confirm=confirm)
            if not confirm:
                check = await call(client.check_workspace_delete, ws)
                if not check.deletable:
                    raise tool_error("workspace_not_empty", f"The workspace '{check.name}' isn't empty, so it can't be "
                                     "deleted here. `blockers` counts what is left.", NEXT_STEP_WORKSPACE_NOT_EMPTY,
                                     workspace=ws, blockers=check.blockers)
                return _preview_by_id("delete_workspace", ws, to_dict(check),
                                      f"Delete the empty workspace '{check.name}' (id `{ws}`)? This can't be undone.")
            result = await call(client.delete_workspace, ws)
        out = to_dict(result)
        out["next_step"] = ("Deletion was accepted. The workspace leaves the workspaces list within a moment and is "
                            "removed in the background. If it still shows briefly, the delete didn't fail, so don't "
                            "delete it again. Its name stays taken for a while, so a new workspace can't use it yet.")
        return out
