# Changelog

All notable changes to the Meshive MCP server. The hosted server at `https://mcp.meshive.ai/mcp` always runs the
latest release; self-hosted images are published to Docker Hub as `meshive/meshive-mcp:<version>`.

## v1.0.0

The first stable release of the Meshive MCP server. Claude Code, Codex, Cursor, Gemini CLI and other MCP clients can
manage Meshive GPUs in plain language: look up GPUs and prices, launch and manage pods, run serverless tasks, serve
models, move files through Asset Hub and check spending. From here on the server follows semantic versioning:
tool names and arguments stay compatible within 1.x.

Docker Hub tags from before 1.0.0 (`0.0.1` and the `dev-<sha>` and `real-<sha>` tags built before this release)
are removed on November 12, 2026. Use `1.0.0` or `latest`.

### Tools (39)

- **Account and billing:** `account`, `billing_history`
- **Workspaces:** `workspaces`
- **GPUs and templates:** `gpus` (works without a key), `templates`
- **Pods:** `pods`, `estimate_pod`, `create_pod`, `start_pod`, `stop_pod`, `restart_pod`, `delete_pod`,
  `ssh_access`, `logs`, `transactions`, `watched_folders`, `set_watched_folders`
- **Storage:** `storages`, `create_storage`, `delete_storage`
- **Serving models:** `models`, `detect_model`, `register_model`, `delete_model`, `servings`, `deploy_serving`,
  `scale_serving`, `pause_serving`, `delete_serving`
- **Serverless tasks:** `tasks`, `estimate_task`, `submit_task`, `stop_task`
- **Files (Asset Hub):** `assets`, `import_asset`, `download_links`, `source_credentials`
- **Hosting:** `machines`
- **Recovering a write:** `operation_status`

### How it keeps you in control

- **Confirmation before spending or deleting.** Tools that create, start, delete or raise the cost of something
  return a price or a summary first, and act only when called again with `confirm=true` after you agree.
- **Safe retries.** Every write carries an `operation_id` that becomes an idempotency key, so a retried call is never
  applied twice, and `operation_status` checks what happened to a write whose answer was lost.
- **Read only keys stay read-only.** Write tools need a **Read & write** key; with a **Read only** key the agent can
  look but not change anything.
- **Secrets go only to you.** Pod logins and SSH passwords are hidden unless the agent asks for one pod's login, and
  the agent is told to hand them only to you.
- **Untrusted text stays data.** Logs and other text from containers are marked as data, so the agent doesn't follow
  instructions printed inside your pods.
- **Amounts as the console shows them.** Money fields come with a `*_display` string rounded the way the console
  rounds it, and sizes use the console's units (GiB, MiB; GPU memory in GB).
- **Clear errors.** Every error has a `code`, a `message` and a `next_step` the agent can follow, such as waiting out
  a rate limit or asking you to top up credit.

### Running it

- Hosted at `https://mcp.meshive.ai/mcp` with your API key in the `Authorization` header. The server is stateless and
  stores nothing.
- Self-hosted with `docker run -p 8080:8080 meshive/meshive-mcp:1.0.0` (amd64 and arm64), or from source with
  `pip install .`. Built on the `meshive` Python SDK 1.x.
