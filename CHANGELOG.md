# Changelog

## Unreleased

- Serving without the console: `detect_model` checks a Hugging Face repo, `register_model` registers it and returns
  the `registration_id` that `deploy_serving` takes, `models` lists registrations with the workspace's Hugging Face
  token IDs, and `delete_model` removes one (with `confirm`). Registering costs nothing — the model downloads when
  deployed — so `register_model` has no `confirm` step, but it still takes an `operation_id` like every write.
  `deploy_serving` now points to `models` instead of the console.
- New read tool `download_links`: temporary download links for an asset's files (optionally narrowed with `paths`
  globs) or a task's output files, with each file's path and size and when the links expire. It never returns file
  contents, and its note tells the agent to hand the links only to the user. Needs a server with the SDK download
  routes; an older one answers that it does not support downloads yet.
- `pods` gives a pod's `endpoints` (URLs with `readiness`) and `connect_credentials`, the logins the console shows on
  Connect — such as the `ACCESS_PASSWORD` that ComfyUI pods now generate, without which a user could not open the
  pod an agent created. Secret values are `null` in every response (including the `start_pod`, `stop_pod` and
  `delete_pod` previews) unless the agent asks for one pod with `show_secrets=true`, which adds a note to hand the
  value only to the user. Pods also carry their state fields (`same_node_unavailable_reason`, `stop_reason_*`,
  `waiting_mode`, `billing_active`, premiums, `is_downloader`), and `start_pod`'s preview says why `same_node` would
  wait or is refused.
- `workspaces` items have `member_role` (`admin`, `billing`, `viewer`).
- `transactions` reports `live` and `phase` (`verifying`, `waiting_for_storage`) while input assets download, and
  `init_logs` when a download failed, with the same untrusted-data note as `logs`.
- `tasks` has the input assets, the asset a download failed on, output upload progress and `outputs_purged_at`.
- `assets` reads assets without versions: `size_bytes`, `file_count`, `upload_status`, `files` and usage on a single
  asset. `version_count`, `latest_version` and `versions` are gone from the output, and `submit_task`'s
  `input_assets` no longer mentions a version.
- Requires `meshive` SDK 0.1.3: the fields above are new in it. The real image build waits for 0.1.3 on PyPI.
- Key errors point to the console's **Settings > API keys** (they said "Settings > Secret", a menu that no longer
  exists). `invalid_api_key` and `write_scope_required` also say how to swap in a new key — in Claude Code,
  `claude mcp remove meshive` first, because `claude mcp add` refuses a name that already exists. The README's agent
  setup matches the docs: Claude Code with `--scope user`, Codex with the key in `http_headers`, Gemini CLI through
  `gemini mcp add --scope user`.
- Sizes follow the console's 1024-based units. `delete_storage` put a GB label on the volume's MiB value, so a 100 GiB
  volume was described as `102400.0 GB`; it now says `100 GiB`. `create_storage` says GiB, and the log and script
  limits say 64 KiB and 256 KiB. Tool descriptions give the units of raw size fields: storage sizes, pod metric sizes
  and a workspace's `ram`/`total_storage` are MiB (the `storages` tool said GB), a machine's metric `ram_size` is
  bytes, and network rates are bytes per second. The server instructions say that `size_gb`, `ram_gb`, `disk_gb`,
  `max_size_gb`, `ram_recommended` and `price_per_gb_month` are GiB, and that only VRAM (`vram_gb`) is said in GB.
- `scale_serving` now asks for `confirm` for **every** change that can raise the hourly cost — a larger replica range,
  turning autoscale on, or a higher per-replica price cap — using the SDK's `Serving.scale_raises_cost` (the CLI uses
  the same rule). Lowering the range, turning autoscale off or lowering the cap still applies immediately.
- `logs` takes `cursor` for tasks on an external provider: omit it for the last `tail` lines (the server now returns the
  newest lines, not the oldest buffered ones), pass the previous response's `next_cursor` to read only new lines. When
  nobody has been watching a pod the server wakes the log watcher before answering; if it cannot, the `note` says the
  lines may be behind.
- `estimate_pod` / `create_pod` no longer take `disk_gb`: the system disk is sized by the server (it always overrode
  the value after the estimate) and the estimate's `resources.disk_gb` shows the real size.
- CI: images are published only after the test and smoke jobs pass (the publish job moved into `ci.yml`). dev images
  pin the SDK to the exact `meshive-python` dev commit resolved at build time instead of the moving `dev` branch, and
  both commits are recorded as image labels (`org.opencontainers.image.revision`, `ai.meshive.sdk.revision`) and in
  `/healthz` (`revision`, `sdk_version`, `sdk_revision`), so what is running can be matched to what was reviewed.

- `start_pod`, `pause_serving` (when resuming) and `scale_serving` (when raising the replica range) now take
  `confirm` like the create/delete tools — resuming or increasing billing needs the user's go-ahead too.
- `name_taken` now tells the agent to check the list tool first when a create did not get a clear answer,
  instead of creating a second resource under another name.
- `create_storage`: `encrypted` applies to `nfs` (network) volumes; `hostPath` volumes cannot be encrypted and the
  server rejects that combination.
- CI: the real image build first checks that a `meshive` SDK release satisfying the `pyproject.toml` pin is on PyPI
  and fails early with the required release order otherwise.
- `logs` now states that log lines are untrusted output of the user's container — in the tool description, in every
  response (`note`) and in the server instructions — so an agent does not follow instructions printed by a container.
- A blank `pod` argument is rejected instead of resolving to an internal system pod, and the system pods used to
  prepare assets are excluded from display-name matching.
- Money fields now come with a `<field>_display` string formatted the way the Meshive web console formats it —
  hourly rates to three decimals (`"$0.068"`), other amounts to two (`"$2.10"`) — and the server instructions tell the
  agent to show it verbatim, so the amount it quotes matches the console. The raw number stays in place for
  arithmetic. Confirmation previews use the same formatting.

- Write tools (need a Read & write key): `create_pod`, `stop_pod`, `start_pod`, `restart_pod`, `delete_pod`,
  `create_storage`, `delete_storage`, `deploy_serving`, `scale_serving`, `pause_serving`, `delete_serving`,
  `submit_task`, `stop_task`, plus read-only `estimate_pod`, `estimate_task` and `logs`. Spending/deleting tools
  require `confirm=true` and otherwise only return an estimate or summary. 409s are mapped to `no_capacity`,
  `name_taken`, `price_exceeds_cap`, `storage_in_use`, `in_progress`, `vram_tier_required`.
- Requires the `meshive` SDK 0.1.x; dev images install it from the SDK's dev branch until it is on PyPI.

- `workspace` accepts a workspace label as well as the id, and `"all"` on pods/storages/servings.
- A wrong workspace id now returns `unknown_workspace` (with candidates) instead of `forbidden`.
- Tool output is compact JSON with `structuredContent`; backend decimal strings like `0E-8` are normalized.

- Initial skeleton: remote Streamable HTTP server (stateless, JSON responses), Bearer API key
  passthrough, 11 read-only tools over the `meshive` SDK, list capping with cursors, model-facing
  error translation, `/healthz`, Dockerfile, CI, Docker Hub image `meshive/meshive-mcp`.
