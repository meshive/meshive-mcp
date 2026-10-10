# Meshive MCP server

[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

Manage your [Meshive](https://meshive.ai) GPUs from Claude Code, Codex, Cursor, Gemini CLI or any other
MCP-capable agent, in plain language. Ask which GPUs are available, launch and stop pods, run serverless tasks,
serve models, read logs and check your spending.

We host it at **`https://mcp.meshive.ai/mcp`**. There is nothing to install: add the URL and your API key to your
agent.

[Documentation](https://docs.meshive.ai/sdk-cli/mcp/) · [Console](https://console.meshive.ai) ·
[Python SDK & CLI](https://github.com/meshive/meshive-python)

## Quick start

**1. Get an API key.** In the [console](https://console.meshive.ai), open your workspace's
**Settings → API keys**. A **Read only** key lets the agent look around; pick **Read & write** if it should also
create, change or delete things.

**2. Add the server to your agent.** For Claude Code:

```bash
claude mcp add --scope user --transport http meshive https://mcp.meshive.ai/mcp --header "Authorization: Bearer meshive_..."
```

`--scope user` makes it available in every folder; without it, only in the folder where you ran the command.
To switch to a new key, run `claude mcp remove meshive` first; `add` refuses a name that already exists.

**3. Check that it works.** Ask your agent *"Which Meshive workspaces do I have?"* It should list them. If it
reports `no_api_key` or `invalid_api_key`, the header is missing or the key was rejected.

<details>
<summary><b>Codex CLI</b></summary>

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.meshive]
url = "https://mcp.meshive.ai/mcp"
http_headers = { "Authorization" = "Bearer meshive_..." }
```

To keep the key out of the file, use `bearer_token_env_var = "MESHIVE_API_KEY"` instead of `http_headers` and set
that variable wherever Codex starts. A variable exported in one terminal isn't seen by other terminals, the IDE
extension or the desktop app.

</details>

<details>
<summary><b>Cursor</b></summary>

In `~/.cursor/mcp.json` (every project) or `.cursor/mcp.json` (one project). If the file already has
`mcpServers`, add just the `meshive` entry:

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

</details>

<details>
<summary><b>Gemini CLI</b></summary>

```bash
gemini mcp add --scope user --transport http --header "Authorization: Bearer meshive_..." meshive https://mcp.meshive.ai/mcp
```

This adds the server to `~/.gemini/settings.json` and leaves your other settings alone. Run it again with a new key
to replace the old one.

</details>

<details>
<summary><b>Other clients</b></summary>

Any client that supports the **Streamable HTTP** transport with custom headers works the same way: URL
`https://mcp.meshive.ai/mcp`, header `Authorization: Bearer meshive_...`.

Clients that only support stdio can bridge with the generic `mcp-remote` shim:

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

</details>

## Things to ask

- *"Which GPUs can I rent right now, and what do they cost per hour?"*
- *"Start a Jupyter pod with an RTX 3060 in my workspace. Tell me the price first."*
- *"My pod has been creating for ten minutes. What is it doing?"*
- *"Show me the last 50 lines of my-pod's logs."*
- *"Stop all running pods in my workspace."*
- *"How much credit do I have left, and what did my workspace spend this week?"*
- *"Give me download links for the outputs of my last task."*

## You stay in control

- **Nothing is spent or deleted without your OK.** Before creating, starting, deleting or raising the cost of
  anything, the agent gets a price or a summary, shows it to you and waits for your go-ahead.
- **A Read only key stays read-only.** With one, the agent can look at everything but change nothing.
- **Logs are treated as data.** Text printed inside your pods is never followed as instructions.
- **Passwords go only to you.** Pod logins and SSH passwords stay hidden unless you ask for them.
- **Your key isn't stored.** The server keeps no state: your key travels with each request and is passed on to the
  Meshive API.

## Tools

| Area | Tools |
| --- | --- |
| Account and billing | `account`, `billing_history` |
| Workspaces | `workspaces` |
| GPUs and templates | `gpus` (also works without a key), `templates` |
| Pods | `pods`, `estimate_pod`, `create_pod`, `start_pod`, `stop_pod`, `restart_pod`, `delete_pod`, `ssh_access`, `logs`, `transactions`, `watched_folders`, `set_watched_folders` |
| Storage | `storages`, `create_storage`, `delete_storage` |
| Serving models | `models`, `detect_model`, `register_model`, `delete_model`, `servings`, `deploy_serving`, `scale_serving`, `pause_serving`, `delete_serving` |
| Serverless tasks | `tasks`, `estimate_task`, `submit_task`, `stop_task` |
| Files (Asset Hub) | `assets`, `import_asset`, `download_links`, `source_credentials` |
| Hosting | `machines` |
| Recovering a write | `operation_status` |

What each tool does, and how confirmation and recovery work:
[documentation](https://docs.meshive.ai/sdk-cli/mcp/).

## Run it yourself

The hosted server is all most people need. To run your own copy:

```bash
docker run -p 8080:8080 meshive/meshive-mcp
```

Then point your agent at `http://localhost:8080/mcp` with the same `Authorization` header. From a clone of this
repository you can also run `pip install .` and then `meshive-mcp` (HTTP on `127.0.0.1:8080`) or
`meshive-mcp --transport stdio`, which reads the key from `MESHIVE_API_KEY`.

| Variable | Meaning |
| --- | --- |
| `MESHIVE_BASE_URL` | Meshive API base URL (defaults to production) |
| `MESHIVE_MCP_HOST`, `MESHIVE_MCP_PORT` | HTTP bind address |
| `MESHIVE_MCP_ALLOWED_HOSTS` | Comma-separated `Host` allowlist; empty turns off DNS-rebinding checks (use behind a reverse proxy) |
| `MESHIVE_API_KEY` | Fallback key, **stdio mode only** |

## Contributing

See [DEVELOPMENT.md](DEVELOPMENT.md).

## License

[Apache License 2.0](LICENSE)
