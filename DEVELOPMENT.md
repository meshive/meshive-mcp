# Development notes

For contributors and anyone running their own deployment: how the server is laid out, how to run the tests, the
rules for tools, and how images are built. Users only need the [README](README.md).

## Layout

| Path | What's in it |
| --- | --- |
| `meshive_mcp/server.py` | Builds the MCP server and the HTTP app: stateless, JSON responses, `/mcp` and `/healthz`. Also holds the instructions every agent receives. |
| `meshive_mcp/tools/` | One module per area, each exposing `register(server)`. `_common.py` has the `meshive_tool` decorator, the annotations and the preview/confirm helpers. |
| `meshive_mcp/client.py`, `meshive_mcp/auth.py` | Builds an SDK client per request from the request's Bearer key, and identifies the calling agent. |
| `meshive_mcp/errors.py` | Turns SDK and network errors into `{code, message, next_step}` for the agent. |
| `meshive_mcp/serialize.py`, `meshive_mcp/money.py` | SDK objects to JSON for the agent: drops `.raw`, hides secrets, and adds `*_display` amounts rounded like the console. |
| `meshive_mcp/paging.py`, `meshive_mcp/workspace.py` | Paging with opaque cursors, and resolving workspace and pod names to IDs. |
| `meshive_mcp/settings.py` | Environment variables (see the README). |

The server is a thin layer over the [`meshive`](https://github.com/meshive/meshive-python) SDK; business rules
live in the API.

## Setup and tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

To work against an SDK change that isn't on PyPI yet, install both in one call:
`pip install -e ../meshive-python -e ".[dev]"`.

The tests drive the tools through an in-process MCP client and through the HTTP transport, with a fake SDK
client, so they need no network and no API key.

## Rules for tools

- Register a tool with `@meshive_tool(server, "name", title="Human Title", annotations=...)`. The title is
  required; a tool without one fails at import.
- **Confirm before spending or deleting.** A write that spends credit, deletes something or can raise the cost
  takes `confirm` (default `false`). With `confirm=false` it returns an estimate or a summary and changes nothing;
  the agent shows it to the user and calls again with `confirm=true`.
- **Stable operation IDs.** Every write takes `operation_id`. A preview returns one, and the confirmed call and
  every retry reuse it; it becomes the SDK's `Idempotency-Key`. A write is only sent when the call supplies an
  `operation_id`. `operation_status` reads an operation's acceptance record without resubmitting it.
- **Untrusted text stays data.** Logs, labels, scripts and error text can come from a user's container or from other
  people. Return them as data, and keep the warning in the tool description, in the response and in the server
  instructions.
- **Secrets stay hidden.** Connect passwords are hidden in every response unless the user asks for one pod with
  `show_secrets=true`. Tools that return a one-time SSH password (`ssh_access`, `connect_storage`) send no
  `Idempotency-Key`, so the API never keeps a replayable copy of the response, and they never return the
  browser-terminal URL, which carries the password.
- Responses are capped at 1 MiB and lists are paged.
- When adding or removing a tool, update the tool sets in `tests/test_tools.py`, the tool table in `README.md` and
  `CHANGELOG.md`.

## Images and releases

`dev` is the default branch, and pull requests go there. `real` holds what is deployed in production.

CI runs the tests, builds the image and smoke-tests it. Pushes to `dev` and `real` then publish to Docker Hub as
`meshive/meshive-mcp` (amd64 and arm64):

| Branch | Tags | SDK inside |
| --- | --- | --- |
| `dev` | `:dev`, `:dev-<short sha>` | The meshive-python `dev` commit at build time |
| `real` | `:latest`, `:<version>`, `:real-<short sha>` | The release from PyPI |

`pyproject.toml` pins a range of `meshive` versions. A `real` build checks first that PyPI has a release in that
range, so release the SDK before merging an MCP change that needs it.

Every push to `real` republishes `:latest` and `:<version>` for the version in `meshive_mcp/_version.py`, so a change
that reaches `real` without a version bump overwrites the `:<version>` image with a different commit. Bump the
version in every change that goes to `real`.

To release a version: bump `meshive_mcp/_version.py`, add its `## vX.Y.Z` section to `CHANGELOG.md`, merge to
`real` and wait for the `:<version>` image, then push the tag `vX.Y.Z` on that commit. The Release workflow checks
that the tag matches the package version and creates the GitHub Release with that changelog section as its notes
(`scripts/release-notes.sh`). Pre-release tags such as `v1.1.0rc1` are marked as pre-releases.

## Verifying a deployment

`GET /healthz` returns `version`, `revision` (the MCP commit), `sdk_version` and `sdk_revision` (the SDK commit,
`null` when the SDK came from PyPI). The same revisions are image labels. Record them together with the running
image digest. `status: "ok"` only says the process is up; it doesn't check the API behind it.
