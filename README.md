# meshive-mcp

Remote [MCP](https://modelcontextprotocol.io) server for the [Meshive](https://meshive.ai) GPU Cloud.
Connect it to Claude Code, Codex, Cursor, or any MCP-capable agent and manage your Meshive
account, workspaces, pods, storage, GPUs, templates, serverless deployments, and hosted machines
in natural language.

The server is a thin tool layer over the [`meshive`](https://pypi.org/project/meshive/) Python
SDK. It holds no state and stores no credentials: your API key travels in the request header and
is forwarded to the Meshive API as-is.

## Connect your agent

You need a Meshive API key. Create one in the [console](https://console.meshive.ai)
(workspace **Settings → Secret**). Keys look like `meshive_` followed by 64 characters.

The server URL is `https://mcp.meshive.ai/mcp`. Every client below sends the key as
`Authorization: Bearer <key>`.

**Claude Code**

```bash
claude mcp add --transport http meshive https://mcp.meshive.ai/mcp \
  --header "Authorization: Bearer meshive_..."
```

**Codex CLI** — add to `~/.codex/config.toml`:

```toml
[mcp_servers.meshive]
url = "https://mcp.meshive.ai/mcp"
bearer_token_env_var = "MESHIVE_API_KEY"
```

and export `MESHIVE_API_KEY=meshive_...` in your shell.

**Cursor / other `mcp.json` clients**

```json
{
  "mcpServers": {
    "meshive": {
      "url": "https://mcp.meshive.ai/mcp",
      "headers": { "Authorization": "Bearer meshive_..." }
    }
  }
}
```

See [docs/clients.md](docs/clients.md) for more clients and for stdio-only clients.

## Tools

All tools in this version are read-only. `gpus` also works without an API key (prices only).

| Tool | What it does |
|---|---|
| `account` | Who the key belongs to, credit balance |
| `workspaces` | List workspaces, or one workspace with cost summary and members |
| `pods` | List pods in a workspace, or one pod (optionally with live metrics) |
| `storages` | Storage volumes of a workspace |
| `gpus` | GPU types available to rent with hourly prices |
| `templates` | Pod templates you can launch from |
| `servings` | Serverless model deployments |
| `tasks` | Serverless one-off GPU jobs |
| `assets` | Asset Hub datasets, models, adapters, outputs |
| `machines` | Machines you host, with earnings and live metrics |
| `billing_history` | Credit top-ups and refunds, or host earnings by day |

Lists are paged (20 per call by default, 100 max) with an opaque `cursor`. Errors come back as
`{"code", "message", "next_step"}` so the agent knows what to do next.

## Run it yourself

```bash
pip install .
meshive-mcp                      # HTTP on 127.0.0.1:8080, endpoint /mcp, health at /healthz
meshive-mcp --transport stdio    # local stdio for development; reads MESHIVE_API_KEY
```

Environment variables:

| Variable | Meaning |
|---|---|
| `MESHIVE_BASE_URL` | Meshive API base URL (defaults to production) |
| `MESHIVE_MCP_HOST`, `MESHIVE_MCP_PORT` | HTTP bind address |
| `MESHIVE_MCP_ALLOWED_HOSTS` | Comma-separated `Host` allowlist; empty disables DNS-rebinding checks (use behind an ingress) |
| `MESHIVE_API_KEY` | Fallback key, **stdio mode only** |

Docker:

```bash
docker build -t meshive-mcp .
docker run -p 8080:8080 meshive-mcp
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The test suite drives the tools through an in-process MCP client and through the HTTP transport
with a fake SDK client, so it needs no network and no API key.

## License

Apache-2.0
