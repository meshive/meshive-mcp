# Meshive MCP server

[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

Manage your [Meshive](https://meshive.ai) GPUs from Claude Code, Codex, Cursor, Gemini CLI or any other
MCP-capable agent, in plain language. Ask which GPUs are available, launch and stop pods, run serverless tasks,
serve models, read logs and check your spending.

Use **`https://mcp.meshive.ai/mcp`** for public GPU prices, access with a Meshive API key, and OAuth sign-in when enabled on the server.

[Documentation](https://docs.meshive.ai/sdk-cli/mcp/) · [Console](https://console.meshive.ai) ·
[Python SDK & CLI](https://github.com/meshive/meshive-python)

## Quick start

### Sign in from an agent

Add the remote URL without a header to check on-demand and spot GPU prices. On an OAuth-enabled deployment, asking for your pods starts sign-in. Review the callback host and signed-in account in the Meshive console. Keep Read for lookups and select Write if you want the app to manage resources. All 71 tools are discoverable. The examples use `meshive-oauth` so you can keep an existing `meshive` connection with a manual key.

```bash
claude mcp add --scope user --transport http meshive-oauth https://mcp.meshive.ai/mcp
```

```bash
codex mcp add meshive-oauth --url https://mcp.meshive.ai/mcp
codex mcp login meshive-oauth
```

For credentials and one-time keys, open the confirmation link and receive the value in the signed-in console. Revoke an OAuth connection under **Account → API keys** to end access and refresh.

### Connect with a manual key

**1. Get an API key.** In the [console](https://console.meshive.ai), open
**Account → API keys**. A **Read only** key lets the agent look around; pick **Read & write** if it should also
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
- *"Give my-pod two GPUs instead of one. What will it cost?"*
- *"Which model sets can I start a ComfyUI pod with?"*
- *"How much credit do I have left, and what did my workspace spend this week?"*
- *"Give me download links for the outputs of my last task."*
- *"Schedule maintenance on my GPU machine for Saturday night, and tell me who gets notified."*
- *"What did my API keys change this week, and was anything refused?"*
- *"Who can reach my-pod's web UI from outside? Make an access key for my script."*

## You stay in control

- **Spending and deleting start with a preview.** Tools that spend or delete return a price or a summary unless the
  agent sets `confirm`, and the agent is told to show it to you and wait for your go-ahead. That step is an instruction
  to the agent: the server can't tell whether you agreed, so give a Read & write key only to an agent you trust.
- **A Read only key stays read-only.** With one, the agent can look at everything but change nothing.
- **You can see what was done.** Every change, check, estimate, login reveal and install command made with your API
  keys is recorded with its result, and reads are counted by the hour; request contents are never recorded. See it on the console's
  **API keys** page under **Activity**, where you can also revoke a key, or ask the agent (`activity`). It is kept
  for 180 days.
- **Logs are treated as data.** Text printed inside your pods, and messages from the people renting your machines,
  are never followed as instructions.
- **Passwords go only to you.** Pod logins and SSH passwords for pods and storage stay hidden unless you ask for
  them. Revealing a hosted machine's login or getting an install command for a new machine also needs your go-ahead,
  and the account owner is emailed each time.
- **Public ports start locked.** A port a new pod exposes over HTTPS is created locked: it lets through workspace
  members signed in to the console and programs that send one of the port's access keys, and answers everyone else
  with 401. The agent opens a port to anyone who knows its URL only when you ask for that, after showing you which
  ports would be open. One kind of port starts open without your asking: a port that its template sets open. The
  agent is told to point it out before the pod is created and to ask whether you want it locked. An access key is
  created only when you ask, its value is shown to you once, and the agent is
  told not to use it or write it down; for its own work in a pod it calls the app from inside the pod instead.
- **Your key isn't stored.** The server keeps no state: your key travels with each request and is passed on to the
  Meshive API.

## Tools

| Area | Tools |
| --- | --- |
| Account and billing | `account`, `activity` (what your API keys did), `billing_history` (credit, host earnings and payouts) |
| Workspaces | `workspaces`, `create_workspace`, `update_workspace`, `delete_workspace` |
| GPUs, CPUs and templates | `gpus` (also works without a key), `cpus`, `templates`, `model_sets` |
| Pods | `pods`, `estimate_pod`, `create_pod`, `update_pod`, `start_pod`, `stop_pod`, `restart_pod`, `delete_pod`, `run_command`, `command_status`, `ssh_access`, `logs`, `transactions`, `watched_folders`, `set_watched_folders` |
| Port locks and access keys | `set_port_lock`, `set_port_preflight`, `revoke_browser_sessions`, `access_keys`, `create_access_key`, `roll_access_key`, `revoke_access_key` |
| Storage | `storages`, `create_storage`, `update_storage`, `connect_storage`, `delete_storage` |
| Serving models | `models`, `detect_model`, `register_model`, `delete_model`, `servings`, `deploy_serving`, `scale_serving`, `pause_serving`, `delete_serving` |
| Serverless tasks | `tasks`, `estimate_task`, `submit_task`, `stop_task` |
| Files (Asset Hub) | `assets`, `upload_asset`, `finish_upload`, `import_asset`, `download_links`, `source_credentials` |
| Hosting | `machines`, `machine_history`, `host_messages`, `send_host_message`, `mark_host_messages_read`, `schedule_maintenance`, `maintenance_window`, `set_machine_listing`, `retry_machine_setup`, `delete_machine`, `machine_credentials`, `host_install_command` |
| Recovering a write | `operation_status` |
| Reporting to the Meshive team | `send_feedback` (a bug, an idea or a complaint; the agent asks you first) |

What each tool does, and how confirmation and recovery work:
[documentation](https://docs.meshive.ai/sdk-cli/mcp/).

## Troubleshooting

When something goes wrong the agent gets an error `code` with a `next_step` and usually tells you what to do. The
common ones:

| The agent reports | What to do |
| --- | --- |
| `no_api_key` | The `Authorization` header isn't reaching the server. Check the header in your agent's MCP settings and restart the agent. |
| `invalid_api_key` | The key is wrong, revoked or expired. Create a new one under **Account → API keys** and replace it (in Claude Code, `claude mcp remove meshive`, then `add` again). |
| `write_scope_required` | The key is **Read only**. Use a **Read & write** key to create, change or delete. |
| `unknown_workspace` | The workspace ID or name doesn't match one of yours. The agent can list them with `workspaces`. |
| `workspace_not_empty` | A key can only delete an empty workspace. The agent tells you what is left. Clear it, or delete the workspace yourself in the console, which removes what's in it for good. |
| `insufficient_credit` | Top up credit in the [console](https://console.meshive.ai). |
| A pod's URL answers 401, or sends your browser to the console | The port is locked, which is the default for new pods. Sign in to the console as a workspace member, or have your program send one of the port's access keys as `Authorization: Bearer <key>`. Ask the agent for a key, or to unlock the port. |
| `endpoint_lock_unavailable` | Ports can't be locked right now, so nothing was created or changed. Try again later, or tell the agent that you want the port open to anyone who knows its URL. |
| `rate_limited` | Too many calls in a short time. The agent waits the number of seconds it was told, then tries again. |
| The agent doesn't see any Meshive tools | Check that the server is listed and connected (`claude mcp list` in Claude Code), then start a new session. |

## Versions

The hosted server always runs the latest release, so there is nothing to upgrade. The server follows
[semantic versioning](https://semver.org): within 1.x, tools keep their names and arguments, and new tools and
options are added alongside. Changes are listed in the [changelog](CHANGELOG.md).

## Run it yourself

The hosted server is all most people need. To run your own copy:

```bash
docker run -p 8080:8080 meshive/meshive-mcp:1.0.0
```

Images on Docker Hub are built for amd64 and arm64. Pin a version tag such as `1.0.0` for a stable deployment;
`latest` follows the newest release. Tags from before 1.0.0 are being removed (see the [changelog](CHANGELOG.md)),
so switch to `1.0.0` or `latest`.

Then point your agent at `http://localhost:8080/mcp` with the same `Authorization` header. From a clone of this
repository you can also run `pip install .` and then `meshive-mcp` (HTTP on `127.0.0.1:8080`) or
`meshive-mcp --transport stdio`, which reads the key from `MESHIVE_API_KEY`.

| Variable | Meaning |
| --- | --- |
| `MESHIVE_BASE_URL` | Meshive API base URL (defaults to production) |
| `MESHIVE_MCP_HOST`, `MESHIVE_MCP_PORT` | HTTP bind address |
| `MESHIVE_MCP_ALLOWED_HOSTS` | Comma-separated `Host` allowlist; empty turns off DNS-rebinding checks (use behind a reverse proxy) |
| `MESHIVE_API_KEY` | Fallback key, **stdio mode only** |

Behind a reverse proxy, keep response buffering off. It can add a `Content-Length` back to short responses, and the
HTTPS scanning in Norton (and in Avast, by user reports) breaks MCP responses that have one. Without request buffering,
slow uploads reach the server as they arrive, so limit concurrent requests per client at the proxy. The server limits
what happens after that: a body must be UTF-8 JSON of up to 1 MiB, 8192 JSON values and 32 levels; 32 requests are
handled at once, and 3 can be open with a body over 64 KiB or of unstated size (others get 503 with `Retry-After`); and a
tool call can have up to 4096 argument values. It raises its open file limit to the hard limit, so the container's
hard `nofile` limit caps how many connections it can hold. Set the container's memory limit from your own load tests.

## Contributing

See [DEVELOPMENT.md](DEVELOPMENT.md).

## License

[Apache License 2.0](LICENSE)
