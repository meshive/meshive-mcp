# Connecting MCP clients

All examples use the hosted server `https://mcp.meshive.ai/mcp` and a key from the
[console](https://console.meshive.ai) (workspace **Settings → Secret**).

## Claude Code

```bash
claude mcp add --transport http meshive https://mcp.meshive.ai/mcp \
  --header "Authorization: Bearer meshive_..."
```

Use `--scope user` to make it available in every project. Check with `claude mcp list`.

## Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.meshive]
url = "https://mcp.meshive.ai/mcp"
bearer_token_env_var = "MESHIVE_API_KEY"
```

Export `MESHIVE_API_KEY` in the shell that launches Codex.

## Cursor

`.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

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

## Gemini CLI

`~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "meshive": {
      "httpUrl": "https://mcp.meshive.ai/mcp",
      "headers": { "Authorization": "Bearer meshive_..." }
    }
  }
}
```

## Kimi Code and other clients

Any client that supports **Streamable HTTP** transport with custom headers works the same way:
URL `https://mcp.meshive.ai/mcp`, header `Authorization: Bearer meshive_...`.

## Clients that only support stdio

Bridge with the generic `mcp-remote` shim; no Meshive-specific install is needed:

```json
{
  "mcpServers": {
    "meshive": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.meshive.ai/mcp",
               "--header", "Authorization: Bearer ${MESHIVE_API_KEY}"]
    }
  }
}
```

## Verifying the connection

Ask the agent: *"Which Meshive workspaces do I have?"* — it should call the `workspaces` tool.
If it reports `no_api_key` or `invalid_api_key`, the header is missing or the key was rejected.
