# Changelog

## Unreleased

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
