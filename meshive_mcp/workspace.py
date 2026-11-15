"""Rules for resolving the workspace argument.

- A given value that is 16 hex characters is used as the id as-is.
- Any other string is treated as a **label** (workspace_name) and its id is looked up — in practice models often put
  the label where the id goes. If not found, `unknown_workspace` is returned with the candidate list.
- "all" means every workspace in list tools (same as the CLI's `pods --all`).
- Omitted: picked automatically if there's 1, an error if 0, and an "ask the user" response with the list if several.
The server stores no default (stateless). The model remembers it within the conversation.
"""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, TypeVar

from meshive import AsyncMeshive
from meshive.models import Workspace

from .errors import invalid_argument, tool_error, translate
from .serialize import to_dict

T = TypeVar("T")

NEEDS_WORKSPACE = "workspace"
ALL = "all"
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_NO_WORKSPACE_STEP = ("Offer to create one with the create_workspace tool (it costs nothing by itself), or the user can "
                      "add one in the console. A new workspace is usable once the workspaces tool shows it `running`.")


async def _read(fn: Callable[..., Awaitable[T]], *args: Any) -> T:
    """One SDK read for a lookup. Tools call these helpers outside their own `call`, so the SDK's errors (a rejected key,
    a rate limit, an unreachable API) are turned into the same coded ToolError here. Anything translate doesn't know
    still raises, so a bug keeps its traceback."""
    try:
        return await fn(*args)
    except Exception as exc:  # noqa: BLE001 — translate re-raises what it doesn't know
        raise translate(exc) from exc


def _summary(workspaces: list[Workspace]) -> list[dict[str, Any]]:
    return [{"workspace": w.namespace_name, "label": w.workspace_name,
             "member_role": w.member_role,
             "owner_emails": list(getattr(w, "owner_emails", None) or [])} for w in workspaces]


def name_key(name: str | None) -> str:
    """Match the server's display-name comparison: lower(trim(name))."""
    return (name or "").strip().lower()


class WorkspaceTargets(list[str]):
    """Workspace ids plus the names already fetched while resolving a list request."""

    def __init__(self, ids: list[str], workspaces: list[Workspace]):
        super().__init__(ids)
        self.names = {w.namespace_name: w.workspace_name for w in workspaces}


def in_workspace(item: Any, workspace: str, targets: WorkspaceTargets) -> dict[str, Any]:
    """Keep call identifiers and add the workspace's display name to each resource row."""
    return {**to_dict(item), "workspace": workspace, "workspace_name": targets.names.get(workspace)}


def _by_label(workspaces: list[Workspace], label: str) -> str:
    wanted = name_key(label)
    hits = [w for w in workspaces if name_key(w.workspace_name) == wanted]
    if len(hits) == 1:
        return hits[0].namespace_name
    if not hits:
        raise tool_error("unknown_workspace", f"No workspace with id or label '{label}'.",
                         "Use one of the `workspace` ids listed here (not the label). "
                         "If unsure, call the workspaces tool and ask the user.",
                         workspaces=_summary(workspaces))
    raise tool_error("ambiguous_workspace", f"Several workspaces are labeled '{label}'.",
                     "Ask the user which one they mean and call again with its `workspace` id.",
                     workspaces=_summary(hits))


async def resolve(client: AsyncMeshive, workspace: str | None, *, required: bool = True) -> str | dict[str, Any] | None:
    """Return a single workspace id. A needs_input dict if one can't be picked from several.

    With required=False, omission passes through as "not specified" (None) (for tools where omission means something, like templates).
    """
    if workspace is not None and workspace.strip():
        workspace = workspace.strip()
        if _ID_RE.match(workspace):
            return workspace
        if workspace.lower() == ALL:
            raise tool_error("invalid_argument", "This tool does not accept workspace='all'.",
                             "Pass a single workspace id, or omit it.")
        return _by_label(await _read(client.list_workspaces), workspace)
    if not required:
        return None
    workspaces = await _read(client.list_workspaces)
    if len(workspaces) == 1:
        return workspaces[0].namespace_name
    if not workspaces:
        raise tool_error("no_workspace", "The user has no workspace.", _NO_WORKSPACE_STEP)
    return {
        "needs_input": NEEDS_WORKSPACE,
        "workspaces": _summary(workspaces),
        "next_step": "The user has several workspaces. Ask which one to use and call again with its "
                     "`workspace` id, or pass workspace='all' to search every workspace.",
    }


async def resolve_many(client: AsyncMeshive, workspace: str | None, *, allow_all: bool = True) -> WorkspaceTargets | dict[str, Any]:
    """For list tools: every id for "all", otherwise the single resolve result as a list."""
    workspaces = await _read(client.list_workspaces)
    if workspace is not None and name_key(workspace) == ALL:
        if not allow_all:
            raise tool_error("invalid_argument", "This tool does not accept workspace='all'.",
                             "Pass a single workspace id or name, or omit it.")
        if not workspaces:
            raise tool_error("no_workspace", "The user has no workspace.", _NO_WORKSPACE_STEP)
        return WorkspaceTargets([w.namespace_name for w in workspaces], workspaces)
    value = (workspace or "").strip()
    if value:
        one = value if _ID_RE.match(value) else _by_label(workspaces, value)
    elif len(workspaces) == 1:
        one = workspaces[0].namespace_name
    elif not workspaces:
        raise tool_error("no_workspace", "The user has no workspace.", _NO_WORKSPACE_STEP)
    else:
        return {"needs_input": NEEDS_WORKSPACE, "workspaces": _summary(workspaces),
                "next_step": "Ask the user which workspace to use, then call again with its `workspace` id."}
    return WorkspaceTargets([one], workspaces)


async def resolve_target(client: AsyncMeshive, workspace: str, *, confirm: bool) -> str:
    """For tools that change the workspace itself (update_workspace, delete_workspace). Unlike `resolve`, nothing is picked
    for the caller: a blank value is refused even when the user has one workspace. A label works on the preview only: it
    stops resolving once the workspace is renamed or deleted, so a confirmed call and its retries go by the id."""
    workspace = (workspace or "").strip()
    if not workspace:
        raise invalid_argument("`workspace` is required — pass the workspace's id or label. This tool never picks one.")
    if _ID_RE.match(workspace):
        return workspace
    if confirm:
        raise tool_error("invalid_argument", f"confirm=true needs the workspace id, not the label '{workspace}'.",
                         "Nothing was sent. Use the id in the preview's `workspace` field in place of the label (call "
                         "without confirm to get it), and keep that id for every retry.")
    return _by_label(await _read(client.list_workspaces), workspace)


def needs_input(value: Any) -> bool:
    return isinstance(value, dict) and "needs_input" in value


_POD_NAME_RE = re.compile(r"^[0-9a-f]{16}-\d+$")


def _is_downloader(pod: Any) -> bool:
    """System pod for asset preparation. Its label (user_alias) is empty."""
    return bool(getattr(pod, "raw", {}).get("isDownloader"))


async def resolve_pod(client: AsyncMeshive, workspace: str, pod: str) -> str:
    """If `pod` is a pod_name (16 hex + "-N") it's used as-is; otherwise it's treated as a label (user_alias) and looked up in the list.
    Observed (2026-09-09): a model looked up a pod it had just created by label, got not_found, and scanned the list again."""
    pod = (pod or "").strip()
    if not pod:
        # An empty string passed as a label matched the **system downloader pod**, whose user_alias is empty, so stop/delete/logs
        # went to that pod. A missing argument is cut off here.
        raise invalid_argument("`pod` is required — pass a pod_name or the pod's display name.")
    if _POD_NAME_RE.match(pod):
        return pod
    pods = await _read(client.list_pods, workspace)
    # Downloaders are left out of label matching too — they were already left out of the candidate list, only matching was off.
    hits = [p for p in pods if not _is_downloader(p) and name_key(p.user_alias) == name_key(pod)]
    if len(hits) == 1:
        return hits[0].pod_name
    candidates = [{"pod_name": p.pod_name, "name": p.user_alias, "status": p.status} for p in (hits or pods)
                  if not _is_downloader(p)]
    if not hits:
        raise tool_error("not_found", f"No pod named '{pod}' in the selected workspace.",
                         "Use one of the `pod_name` values listed here, or call the pods tool.", pods=candidates)
    raise tool_error("ambiguous_pod", f"Several pods are named '{pod}'.",
                     "Ask the user which one and call again with its `pod_name`.", pods=candidates)


async def resolve_storage(client: AsyncMeshive, workspace: str, storage: str, *, confirm: bool = False) -> str:
    """Accept a storage id or resolve its display name within the selected workspace."""
    if not isinstance(storage, str):
        raise invalid_argument("`storage` must be a pv_name or display-name string.")
    storage = (storage or "").strip()
    if not storage:
        raise invalid_argument("`storage` is required — pass a pv_name or the storage's display name.")
    if _ID_RE.match(storage):
        return storage
    storages = await _read(client.list_storages, workspace)
    # Exact ids also support volumes made by older servers with a different id format.
    if any(s.pv_name == storage for s in storages):
        return storage
    hits = [s for s in storages if name_key(s.user_alias) == name_key(storage)]
    if not hits and re.fullmatch(r"pv-[a-zA-Z0-9-]+", storage):
        # Older server ids remain usable after deletion too, for idempotent retries.
        return storage
    if confirm:
        raise tool_error("invalid_argument", "confirm=true needs the storage's pv_name, not its display name.",
                         "Nothing was sent. Call without confirm to preview by name, then use the returned pv_name "
                         "for confirmation and every retry so a renamed or deleted volume cannot change the target.")
    if len(hits) == 1:
        return hits[0].pv_name
    candidates = [{"pv_name": s.pv_name, "name": s.user_alias, "status": s.status} for s in (hits or storages)]
    if not hits:
        raise tool_error("not_found", f"No storage named '{storage}' in the selected workspace.",
                         "Use a storage name or a `pv_name` from the storages tool.", storages=candidates)
    raise tool_error("ambiguous_storage", f"Several storage volumes are named '{storage}'.",
                     "Ask the user which one and call again with its `pv_name`.", storages=candidates)
