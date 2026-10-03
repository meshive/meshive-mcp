from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from ..client import meshive_client
from ..paging import paginate
from ..serialize import to_dict
from ..workspace import ALL, needs_input, resolve, resolve_many
from ._common import READ_ONLY, call, meshive_tool


def register(server: MCPServer) -> None:
    @meshive_tool(server, "servings", annotations=READ_ONLY)
    async def servings(ctx: Context[Any, Any], workspace: str | None = None, serving: int | None = None,
                       limit: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        """List the serverless model deployments (servings) in a workspace, or show one by `serving_id`.
        Each item has replica counts, status, the inference `endpoint_url`, and the current hourly price in USD.
        `workspace` takes the id from the workspaces tool (a label also works); pass "all" for every workspace, or omit it if the user has only one."""
        async with meshive_client(ctx) as client:
            if serving is not None:
                return {"serving": to_dict(await call(client.get_serving, serving))}
            targets = await resolve_many(client, workspace)
            if needs_input(targets):
                return targets
            items = []
            for ws in targets:
                items.extend(await call(client.list_servings, ws))
        page = paginate([to_dict(s) for s in items], limit, cursor)
        page["workspace"] = ALL if len(targets) > 1 else targets[0]
        return page

    @meshive_tool(server, "models", annotations=READ_ONLY)
    async def models(ctx: Context[Any, Any], workspace: str | None = None) -> dict[str, Any]:
        """List the models a workspace registered for serving, and its Hugging Face tokens.
        Each model's `registration_id` is what deploy_serving takes. `hf_tokens` lists the workspace's tokens by id and label (never the token itself); pass an id as `hf_token_id` to detect_model or register_model for a private repo. Tokens are added in the console.
        `workspace` takes the id from the workspaces tool (a label also works); omit it if the user has only one."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            items = await call(client.list_models, ws)
            tokens = await call(client.list_hf_tokens, ws)
        return {"items": [to_dict(m) for m in items], "hf_tokens": [to_dict(t) for t in tokens], "workspace": ws}

    @meshive_tool(server, "detect_model", annotations=READ_ONLY)
    async def detect_model(ctx: Context[Any, Any], huggingface_repo: str, workspace: str | None = None,
                           hf_token_id: int | None = None) -> dict[str, Any]:
        """Check whether a Hugging Face repo (e.g. "Qwen/Qwen3-0.6B") can be served before registering it; nothing is created.
        `status` is ok, unsupported (see `detail`; `suggested_repo` may name an original repo to try instead of a GGUF) or failed (the repo could not be read — check the name, or pass `hf_token_id` for a private repo).
        `context_length` is the model's detected maximum context."""
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            result = await call(client.detect_model, huggingface_repo, workspace=ws, hf_token_id=hf_token_id)
        out = to_dict(result)
        out["workspace"] = ws
        return out
