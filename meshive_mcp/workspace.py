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
from typing import Any

from meshive import AsyncMeshive
from meshive.models import Workspace

from .errors import invalid_argument, tool_error

NEEDS_WORKSPACE = "workspace"
ALL = "all"
_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _summary(workspaces: list[Workspace]) -> list[dict[str, str]]:
    return [{"workspace": w.namespace_name, "label": w.workspace_name} for w in workspaces]


def _by_label(workspaces: list[Workspace], label: str) -> str:
    wanted = label.strip().lower()
    hits = [w for w in workspaces if (w.workspace_name or "").lower() == wanted]
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
        return _by_label(await client.list_workspaces(), workspace)
    if not required:
        return None
    workspaces = await client.list_workspaces()
    if len(workspaces) == 1:
        return workspaces[0].namespace_name
    if not workspaces:
        raise tool_error("no_workspace", "The user has no workspace.",
                         "Ask the user to create a workspace in the console first.")
    return {
        "needs_input": NEEDS_WORKSPACE,
        "workspaces": _summary(workspaces),
        "next_step": "The user has several workspaces. Ask which one to use and call again with its "
                     "`workspace` id, or pass workspace='all' to search every workspace.",
    }


async def resolve_many(client: AsyncMeshive, workspace: str | None) -> list[str] | dict[str, Any]:
    """For list tools: every id for "all", otherwise the single resolve result as a list."""
    if workspace is not None and workspace.strip().lower() == ALL:
        workspaces = await client.list_workspaces()
        if not workspaces:
            raise tool_error("no_workspace", "The user has no workspace.",
                             "Ask the user to create a workspace in the console first.")
        return [w.namespace_name for w in workspaces]
    one = await resolve(client, workspace)
    if isinstance(one, dict):
        return one
    return [one]  # type: ignore[list-item]


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
    pods = await client.list_pods(workspace)
    # Downloaders are left out of label matching too — they were already left out of the candidate list, only matching was off.
    hits = [p for p in pods if not _is_downloader(p) and (p.user_alias or "").lower() == pod.lower()]
    if len(hits) == 1:
        return hits[0].pod_name
    candidates = [{"pod_name": p.pod_name, "name": p.user_alias, "status": p.status} for p in pods
                  if not _is_downloader(p)]
    if not hits:
        raise tool_error("not_found", f"No pod named '{pod}' in workspace {workspace}.",
                         "Use one of the `pod_name` values listed here, or call the pods tool.", pods=candidates)
    raise tool_error("ambiguous_pod", f"Several pods are named '{pod}'.",
                     "Ask the user which one and call again with its `pod_name`.", pods=candidates[:20])
