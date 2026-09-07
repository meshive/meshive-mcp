# Changelog

## Unreleased

- `workspace` accepts a workspace label as well as the id, and `"all"` on pods/storages/servings.
- A wrong workspace id now returns `unknown_workspace` (with candidates) instead of `forbidden`.
- Tool output is compact JSON with `structuredContent`; backend decimal strings like `0E-8` are normalized.

- Initial skeleton: remote Streamable HTTP server (stateless, JSON responses), Bearer API key
  passthrough, 11 read-only tools over the `meshive` SDK, list capping with cursors, model-facing
  error translation, `/healthz`, Dockerfile, CI, Docker Hub image `meshive/meshive-mcp`.
