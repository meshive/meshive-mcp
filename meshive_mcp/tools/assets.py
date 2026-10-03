from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import clamp_limit, decode_cursor, encode_cursor
from ..serialize import to_dict
from ..workspace import needs_input, resolve
from ..errors import invalid_argument
from ._common import READ_ONLY, call, meshive_tool

_LINKS_NOTE = ("These links are temporary and work for anyone who has one until they expire. Give them only to the "
               "user; they can also run `meshive asset-download` or `meshive task-outputs --download` to save files.")


def register(server: MCPServer) -> None:
    @meshive_tool(server, "assets", annotations=READ_ONLY)
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
            # 백엔드는 page/pageSize. 커서 offset 은 항상 size 의 배수로만 발급되므로 page 로 환산된다.
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

    @meshive_tool(server, "download_links", annotations=READ_ONLY)
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
                        # 서명 일부가 실패하면 files 가 expected 보다 적다 — 다시 부르면 된다.
                        "complete": d.complete, "expires_at": expires_at.isoformat(timespec="seconds"),
                        "note": _LINKS_NOTE}
            outs = await call(client.task_outputs, task)
        # task 결과물 링크의 TTL 은 서버가 응답에 싣지 않는다(갤러리와 같은 몇 시간) — 모르는 값은 null.
        return {"task_id": task, "files": [to_dict(f) for f in outs.files], "expired": outs.expired,
                "expires_at": None, "note": _LINKS_NOTE + " Task output links last a few hours."}
