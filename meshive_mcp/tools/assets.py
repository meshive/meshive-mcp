from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import clamp_limit, decode_cursor, encode_cursor
from ..serialize import to_dict
from ..workspace import needs_input, resolve
from ..errors import invalid_argument
from ._common import CREATE, READ_ONLY, call, meshive_tool

_LINKS_NOTE = ("These links are temporary and work for anyone who has one until they expire. Give them only to the "
               "user; they can also run `meshive asset-download` or `meshive task-outputs --download` to save files.")


def register(server: MCPServer) -> None:
    @meshive_tool(server, "assets", title="List Assets", annotations=READ_ONLY)
    async def assets(ctx: Context[Any, Any], workspace: str | None = None, asset: str | None = None,
                     asset_type: str | None = None, status: str | None = None,
                     limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the Asset Hub assets (datasets, models, adapters, outputs) of a workspace, or show one by `asset_id` ("asset_...").
        The list also reports managed storage usage and the estimated monthly storage cost in USD.
        A single asset lists its `files` and where it is in use. `size_bytes`/`file_count` null means a linked source not measured yet.
        Filter with `asset_type` or `status`. `workspace` takes the id from the workspaces tool (a label also works); omit it if the user has only one."""
        async with meshive_client(ctx) as client:
            if asset is not None:
                return {"asset": to_dict(await call(client.get_asset, asset))}
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            size = clamp_limit(limit)
            offset = decode_cursor(cursor)
            # The backend takes page/pageSize. Cursor offsets are only ever issued as multiples of size, so they convert to a page.
            page_no = offset // size + 1
            page = await call(client.list_assets, ws, asset_type=asset_type, status=status,
                              page=page_no, page_size=size)
            storage = await call(client.get_asset_storage, ws)
        next_offset = offset + len(page.items)
        return {
            "items": [to_dict(a) for a in page.items],
            "total": page.total,
            "next_cursor": encode_cursor(next_offset) if next_offset < page.total else None,
            "storage": to_dict(storage),
            "workspace": ws,
        }

    @meshive_tool(server, "download_links", title="Get Download Links", annotations=READ_ONLY)
    async def download_links(ctx: Context[Any, Any], asset: str | None = None, task: str | None = None,
                             paths: list[str] | None = None) -> dict[str, Any]:
        """Get temporary download links for an asset's files (`asset`, "asset_...") or a task's output files (`task`, "task_...").
        Each file comes with its `path`, `size_bytes` and `url`, plus when the links expire; file contents are never returned.
        Narrow an asset to some files with `paths` globs such as ["config/*", "*.safetensors"]. Linked assets (fetched from Hugging Face or CivitAI when a pod starts) have no download links."""
        if bool(asset) == bool(task):
            raise invalid_argument("Pass exactly one of `asset` or `task`.")
        async with meshive_client(ctx) as client:
            if asset:
                d = await call(client.asset_download_urls, asset, paths=paths)
                expires_at = datetime.now(timezone.utc) + timedelta(seconds=d.expires_in)
                return {"asset_id": d.asset_id, "name": d.name, "files": [to_dict(f) for f in d.files],
                        # If some signatures fail, files has fewer than expected — calling again fixes it.
                        "complete": d.complete, "expires_at": expires_at.isoformat(timespec="seconds"),
                        "note": _LINKS_NOTE}
            outs = await call(client.task_outputs, task)
        # The server doesn't send the TTL of task output links (a few hours, like the gallery) — unknown values are null.
        return {"task_id": task, "files": [to_dict(f) for f in outs.files], "expired": outs.expired,
                "expires_at": None, "note": _LINKS_NOTE + " Task output links last a few hours."}

    @meshive_tool(server, "import_asset", title="Import Asset", annotations=CREATE)
    async def import_asset(ctx: Context[Any, Any], source: str, workspace: str | None = None, name: str | None = None,
                           asset_type: str | None = None, revision: str | None = None, paths: list[str] | None = None,
                           hf_token_id: int | None = None, civitai_key_id: int | None = None,
                           operation_id: str | None = None) -> dict[str, Any]:
        """Link a Hugging Face repo ("owner/name" or its URL), a CivitAI model URL or a direct file URL as an Asset Hub asset, so pods and tasks can use it as `input_assets`.
        Nothing is copied: the asset is ready at once and has no storage charge; pods and tasks download it from the source when they start. `paths` globs link only some files, `revision` pins a Hugging Face branch/tag/commit.
        A private or gated source needs a token or key saved in the console — pass its id from source_credentials; if the import fails, tell the user the error message as it is."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            result = await call(client.import_asset, source, workspace=ws, name=name, asset_type=asset_type,
                                revision=revision, paths=paths, hf_token_id=hf_token_id, civitai_key_id=civitai_key_id)
        out = to_dict(result)
        out["next_step"] = "Imported. Use asset_id in input_assets of submit_task, or look at its files with the assets tool."
        return out
