from __future__ import annotations

import re
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import clamp_limit, decode_cursor, encode_cursor
from ..serialize import to_dict
from ..workspace import in_workspace, needs_input, resolve_many
from ._common import READ_ONLY, call, meshive_tool

_FINISHED = frozenset({"succeeded", "failed", "timed_out", "stopped"})
# Older servers send only the raw upload fields. Some tasks save /outputs without a separate upload step after the
# run: their `output_upload_state` is "skipped" (or null on older tasks) and the upload file counts stay 0 even when
# files were saved, which agents read as "nothing was saved". The task's output list is the reliable check there.
_UNTRACKED_UPLOAD_NOTE = (
    "A finished task whose `output_upload_state` is \"skipped\" or null has no tracked upload step: its "
    "`output_files_completed`/`output_files_declared` stay 0 even when files were saved, so don't tell the user "
    "nothing was saved. Call download_links with task=<task_id> to see the saved files. The list can keep growing "
    "for a few minutes after the task ends; an empty list after that means no files are kept for the task (none "
    "were saved, or they were deleted since). Saved files usually also become an asset in Assets about 30 minutes "
    "after the task ends.")

# The server's `outputs` object (camelCase) → the field names the SDK uses for it.
_OUTPUTS_FIELDS = {
    "phase": "phase", "assetExternalId": "asset_id", "assetName": "asset_name", "assetDeleted": "asset_deleted",
    "filesSaved": "files_saved", "filesNotSaved": "files_not_saved", "filesSoFar": "files_so_far",
    "filesTotal": "files_total", "filesOverLimit": "files_over_limit", "filesLimit": "files_limit",
    "bytesSaved": "bytes_saved", "bytesSoFar": "bytes_so_far", "bytesTotal": "bytes_total",
    "expectedInAssetsAt": "expected_in_assets_at", "uploadStartedAt": "upload_started_at",
    "uploadDeadlineAt": "upload_deadline_at", "nextAttemptAt": "next_attempt_at", "attempts": "attempts",
    "lastError": "last_error", "savedAt": "saved_at",
}
# Raw fields that `outputs` replaces. On a task without a tracked upload step they read "skipped, 0 files" for files
# that were saved, and `outputs_purged_at` (when the task's log was deleted) reads like the outputs were deleted, so
# they're left out whenever `outputs` is there to say what happened.
_REPLACED_FIELDS = ("output_upload_state", "output_files_declared", "output_files_completed",
                    "output_bytes_declared", "output_bytes_completed", "output_upload_last_error",
                    "outputs_purged_at")
_SAVING_PHASES = frozenset({"preparing", "uploading", "retrying", "registering"})


def _untracked_upload(task: Any) -> bool:
    return task.status in _FINISHED and task.output_upload_state in (None, "skipped")


def _size(n: int) -> str:
    for unit, step in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{n} B"


def _num(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _files(n: int) -> str:
    return f"{n:,} file" if n == 1 else f"{n:,} files"


def _asset(o: dict[str, Any]) -> str | None:
    if not o.get("asset_id"):
        return None
    return f"{o['asset_name']} ({o['asset_id']})" if o.get("asset_name") else str(o["asset_id"])


def _progress(o: dict[str, Any]) -> str:
    so_far = _num(o.get("files_so_far")) or 0
    to_upload = _num(o.get("files_to_upload"))
    files = (f"{so_far:,} of {_files(to_upload)}" if to_upload is not None
             else f"{_files(so_far)} so far (total not known yet)")
    bytes_so_far, bytes_total = _num(o.get("bytes_so_far")), _num(o.get("bytes_total"))
    if bytes_total is not None:
        return f"{files}, {_size(bytes_so_far or 0)} of {_size(bytes_total)}"
    return f"{files}, {_size(bytes_so_far)}" if bytes_so_far else files


def _limit(o: dict[str, Any]) -> str:
    limit = _num(o.get("files_limit"))
    return f"{limit:,}-file limit" if limit else "file limit"


def _over_limit(o: dict[str, Any]) -> str:
    over = _num(o.get("files_over_limit")) or 0
    if not over:
        return ""
    return f" {_files(over)} {'was' if over == 1 else 'were'} over the {_limit(o)} and not saved."


def _when(value: Any) -> str:
    """An ISO time without the fraction of a second ("2026-11-05T08:21:56.737030Z" → "2026-11-05T08:21:56Z")."""
    return re.sub(r"\.\d+(?=Z|[+-]\d\d:?\d\d$|$)", "", str(value))


def _see_error(o: dict[str, Any]) -> str:
    return " (see last_error)" if o.get("last_error") else ""


def _summary(o: dict[str, Any]) -> str | None:
    """One line on where saving the outputs stands, from the server's `outputs` — what the console shows."""
    phase = o.get("phase")
    asset = _asset(o)
    if phase == "running":
        return "The task hasn't finished; its /outputs files are saved after it ends."
    if phase == "preparing":
        return "Getting ready to save the output files." + _over_limit(o)
    if phase == "uploading":
        return f"Saving the output files: {_progress(o)}." + _over_limit(o)
    if phase == "retrying":
        when = f" at {_when(o['next_attempt_at'])}" if o.get("next_attempt_at") else ""
        return (f"An upload attempt failed{_see_error(o)} and is retried automatically{when}; "
                f"uploaded so far: {_progress(o)}." + _over_limit(o))
    if phase == "registering":
        when = f", expected around {_when(o['expected_in_assets_at'])}" if o.get("expected_in_assets_at") else ""
        so_far, to_upload = _num(o.get("files_so_far")) or 0, _num(o.get("files_to_upload"))
        if to_upload is not None and so_far < to_upload:
            # A failed upload that got some files up is registered too: the rest won't be saved.
            return (f"The upload ended before every file was saved{_see_error(o)}; the {so_far:,} of "
                    f"{_files(to_upload)} that were uploaded are being added to Assets{when}." + _over_limit(o))
        return f"The uploaded files are being added to Assets{when}: {_progress(o)}." + _over_limit(o)
    if phase == "saved":
        if o.get("asset_deleted"):
            return "The outputs were saved as an asset that has since been deleted."
        saved, size = _num(o.get("files_saved")), _num(o.get("bytes_saved"))
        detail = f": {_files(saved)}, {_size(size or 0)}" if saved is not None else ""
        return f"Saved in Assets as {asset}{detail}." if asset else f"Saved in Assets{detail}."
    if phase == "partially_saved":
        where = (f"Partly saved in Assets as {asset}" if asset else
                 "Partly saved as an asset that has since been deleted" if o.get("asset_deleted")
                 else "Partly saved in Assets")
        saved, missing = _num(o.get("files_saved")), _num(o.get("files_not_saved"))
        counts = ""
        if saved is not None:
            counts = f": {_files(saved)} saved" + (f", {missing:,} not saved" if missing else "")
        over = _num(o.get("files_over_limit")) or 0
        if over:
            rest = ", the rest didn't upload" if missing is not None and missing > over else ""
            why = f" ({over:,} over the {_limit(o)}{rest})"
        else:
            why = " (they didn't upload)" if missing else ""
        return f"{where}{counts}{why}."
    if phase == "not_saved":
        return f"The output files were not saved{_see_error(o)}." + _over_limit(o)
    if phase == "discarded":
        if asset:
            return f"Saving the output files was discarded on request. Files uploaded before that are in Assets as {asset}."
        uploaded = _num(o.get("files_saved")) or 0
        if uploaded and not o.get("asset_deleted") and not o.get("saved_at"):
            return (f"Saving the output files was discarded on request. The {_files(uploaded)} uploaded before that "
                    "are being added to Assets.")
        return "Saving the output files was discarded on request."
    if phase == "empty":
        return "There were no output files to save."
    return None


def _outputs(task: Any) -> dict[str, Any] | None:
    """The server's `outputs` status for a task, in the SDK's field names, or None from a server without it."""
    raw = getattr(task, "raw", None)
    src = raw.get("outputs") if isinstance(raw, dict) else None
    if not isinstance(src, dict):
        return None
    o: dict[str, Any] = {name: src.get(key) for key, name in _OUTPUTS_FIELDS.items()}
    total = _num(o["files_total"])
    # files_total counts the files over the limit too; bytes_total already counts only the files that are uploaded.
    o["files_to_upload"] = max(total - (_num(o["files_over_limit"]) or 0), 0) if total is not None else None
    o["saving"] = o["phase"] in _SAVING_PHASES
    o["summary"] = _summary(o)
    return o


def _task_dict(task: Any) -> dict[str, Any]:
    out = to_dict(task)
    # Built from the response itself, so it reads the same whether or not the SDK has a `Task.outputs` field yet.
    out.pop("outputs", None)
    outputs = _outputs(task)
    if outputs is not None:
        for name in _REPLACED_FIELDS:
            out.pop(name, None)
        out["outputs"] = outputs
    return out


def _needs_upload_note(task: Any) -> bool:
    return _outputs(task) is None and _untracked_upload(task)


def register(server: MCPServer) -> None:
    @meshive_tool(server, "operation_status", title="Check Operation Status", annotations=READ_ONLY)
    async def operation_status(ctx: Context[Any, Any], operation_id: str, method: str, path: str) -> dict[str, Any]:
        """Check a write's durable acceptance record without submitting it again. Use operation_id and
        operation_lookup.method/path from the write result/error. Examples: submit_task=POST /tasks,
        create_pod=POST /pods. done means the API response was saved; poll the returned task/transaction
        for actual execution. pending/unknown require reconciliation, never a new operation_id."""
        async with meshive_client(ctx) as client:
            return await call(client.get_operation, operation_id, method=method, path=path)

    @meshive_tool(server, "tasks", title="List Tasks", annotations=READ_ONLY)
    async def tasks(ctx: Context[Any, Any], workspace: str | None = None, task: str | None = None,
                    status: str | None = None, limit: int | None = None,
                    cursor: str | None = None) -> dict[str, Any]:
        """List serverless tasks (one-off GPU jobs) in a workspace, or show one by `task_id` (looks like "task_...").
        Filter with `status` (queued, scheduling, pulling, fetching, running, succeeded, failed, timed_out, stopped); the
        detail view includes cost and exit code. A task usually goes queued → scheduling → running → succeeded or failed;
        pulling and fetching show only while an image or the input assets take a while to download.
        `outputs` says where saving the task's /outputs files stands, as the console shows it; tell the user its
        `summary`. Its `phase` is running until the task ends; preparing, uploading, retrying or registering while the
        files are being saved (progress is `files_so_far` of `files_to_upload`); then saved (in Assets as
        `asset_name`/`asset_id`), partially_saved, not_saved, discarded or empty.
        A server without `outputs` sends `output_upload_state` instead: "skipped" or null with 0 files doesn't mean
        nothing was saved — check with download_links(task=...).
        `workspace` takes the id from the workspaces tool (a label also works); omit it if the user has only one."""
        async with meshive_client(ctx) as client:
            if task is not None:
                detail = await call(client.get_task, task)
                out: dict[str, Any] = {"task": _task_dict(detail)}
                if _needs_upload_note(detail):
                    out["outputs_note"] = _UNTRACKED_UPLOAD_NOTE
                return out
            targets = await resolve_many(client, workspace, allow_all=False)
            if needs_input(targets):
                return targets
            ws = targets[0]
            size = clamp_limit(limit)
            offset = decode_cursor(cursor)
            # The only list the backend supports limit/offset for — fetch one extra to know whether there's a next page.
            items = await call(client.list_tasks, ws, status=status, limit=size + 1, offset=offset)
        has_more = len(items) > size
        items = items[:size]
        result: dict[str, Any] = {
            "items": [in_workspace(_task_dict(t), ws, targets) for t in items],
            "next_cursor": encode_cursor(offset + size) if has_more else None,
            "workspace": ws,
        }
        if any(_needs_upload_note(t) for t in items):
            result["outputs_note"] = _UNTRACKED_UPLOAD_NOTE
        return result
