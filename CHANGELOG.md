# Changelog

All notable changes to the Meshive MCP server. The hosted server at `https://mcp.meshive.ai/mcp` always runs the
latest release; self-hosted images are published to Docker Hub as `meshive/meshive-mcp:<version>`.

## v1.5.1

Connecting no longer takes 25 seconds, and the server now works on Windows PCs running Avast or Norton with HTTPS
scanning on. Tool names and arguments are unchanged.

### Fixed

- Claude Code took about 25 seconds to connect. The server no longer offers `subscriptions/listen`: its tools don't
  change while it runs, so `server/discover` now reports `listChanged: false` and clients go straight to `tools/list`.
- Clients could not connect, or lost short tool calls, with errors such as `InvalidHTTPResponse` or
  `Invalid character in chunk size`. Avast and Norton rewrite MCP responses that carry a `Content-Length`, so responses
  are now sent chunked.

### Self-hosting

- Behind a reverse proxy, keep response buffering off: it adds the `Content-Length` back to short responses.

## v1.5.0

A task can now keep only some of the files it writes to `/outputs`, as in the console's submit form. Built on the
`meshive` Python SDK 1.5. Tool names and existing arguments are unchanged.

### New

- `estimate_task` and `submit_task` take `output_include`: globs relative to `/outputs` (`["*.safetensors"]` at any
  depth, `["samples/*.png"]` in that folder, `["checkpoints/"]` every folder of that name with its contents). Only
  matching files are uploaded; omitted, every file is, as before. Files left out still use the `/outputs` disk while
  the task runs.
- Tasks report `output_include` (null = no filter, every file). If a filter was passed and the submitted task reports
  null, the server predates the filter and uploads every file.

## v1.4.0

Pods made from templates without an output folder (for example NIM or vLLM) can now save files from the user's own
folders, and a template's output folder can be moved when a pod is created. Built on the `meshive` Python SDK 1.4.
Tool names and existing arguments are unchanged.

### New

- `estimate_pod` and `create_pod` take `watched_folders` on templates without an output folder too, as long as the
  pod has storage the folders sit in. `watched_template_folders` takes `"path"` to move an output folder
  (`{"/workspace/output": {"path": "/workspace/results"}}`); `$MESHIVE_OUTPUT_DIR` and its storage follow.
- `update_pod` takes `enable_folder_saving`: it adds the uploader to an older pod that has none, so its folders can be
  watched (one restart). `watched_folders` reports `can_enable_saving` when that is possible.
- `set_watched_folders` explains how to save another folder instead of a template folder on a running pod (turn the
  template folder off and add the new one).

## v1.3.0

Quick Deploy model sets that are gated on Hugging Face can now be deployed with the user's own Hugging Face token, as
in the console's Quick Deploy panel. Built on the `meshive` Python SDK 1.3. Tool names and existing arguments are
unchanged.

### New

- `estimate_pod` and `create_pod` take `hf_token_id`: for a Quick Deploy template whose model set is gated on Hugging
  Face, the id of a workspace token (from `source_credentials`) whose Hugging Face account accepted the model's
  license. The set's files download with that token. Like `model_set`, a confirmed create with it returns only after
  the files were linked.
- `model_sets` reports `hf_token_required` and `gated_sources` (the page where each repo's license is accepted) per
  set and `hf_gated` per file, and its `next_step` tells the agent where the user accepts the license.
- New error codes with next steps: `hf_token_required`, `hf_license_not_accepted`, `hf_access_not_approved`,
  `hf_token_rejected` and `model_set_needs_token` (a gated set passed as `model_set` on another template).

## v1.2.0

Pods and storage can now be edited the way the console edits them, storage can be opened over SSH, and pods can start
with a model set. Built on the `meshive` Python SDK 1.2. Tool names and existing arguments are unchanged.

### New

- **`update_pod` tool:** change an existing pod like the console's Edit, passing only what changes: name, template,
  rental type, GPU or CPU line and size, variables, ports, start command and storage (attach, detach or create local
  storage). The preview lists the changes, the hourly price before and after and whether the pod restarts. A running
  pod restarts with the new settings; a stopped pod uses them at its next start; a rename alone doesn't restart it.
  The pod type, premiums and image can't change, and template variables and ports can't be removed.
- **Moving a pod for an edit:** when the pod's machine can't fit the change, `update_pod` returns
  `node_migration_required` and changes nothing. Calling again with `allow_node_migration` needs the user's agreement,
  and for a pod with unpreserved workspace files also `allow_data_loss`, the same separate consent as moving a pod
  with `start_pod`.
- **`update_storage` tool:** rename a volume, grow it (it can't shrink) or set its usage warning. The preview shows the
  new size and the hourly price before and after.
- **`connect_storage` tool:** an SSH login to a volume's files, like the console's Connect. Encrypted storage is
  unlocked and locks again by itself after a set time. The one-time password is handled like `ssh_access`: only when
  you ask, only to you, and never kept as a replayable operation record.
- **`model_sets` tool and `model_set` argument:** list the model sets a template recommends and pass one to
  `estimate_pod` and `create_pod`. The estimate shows the set's file count, size and minimum GPU memory. Creating
  with a set fetches its files first and can take several minutes; if a file can't be added, no pod is created and the
  files already added are reused next time.
- **Clear errors for edits and model sets:** each refusal comes with a code and a next step, for example
  `nothing_to_change`, `pod_type_locked`, `template_env_not_removable`, `storage_not_attached`,
  `local_storage_on_another_machine`, `cannot_change_resources`, `harvest_roots_orphaned` (with the watched folders
  to confirm), `storage_cannot_shrink`, `model_set_unavailable` (with the files that failed) and
  `model_set_not_for_template` (with the sets that fit).
- **Watched folders and input assets as on the console's create screen:** `estimate_pod` and `create_pod` take
  `watched_template_folders` to turn off the template's own watched folders or change their patterns from the start.
  A `watched_folders` entry can be added turned off (`"enabled": false`), and an input asset with `paths` takes
  `"strip_prefix": ""` to keep the chosen files' folder structure.

### Changed

- The instructions every agent receives now cover editing pods and storage, Connect and model sets.
- An `input_assets` entry with a key other than `asset`, `target_dir`, `role`, `paths` and `strip_prefix` is now
  refused instead of silently ignored (which attached the whole asset). Changing the template in `update_pod` lists
  the variables, ports and command that don't carry over in the preview.
- `name_taken` and `local_volume_not_allowed` also say what to do when renaming a pod or a volume, or editing a pod into a spot pod.
- An `input_assets` `paths` entry that matches no file is refused instead of skipped, and a Korean or other accented
  name typed in a different Unicode form (NFC or NFD) than the stored file name now attaches that file.
- `pods` with `show_secrets` says when the login came back as `***`: the server now hides secret values from
  read-only API keys, so the user needs a key with write scope.

## v1.1.0

Pods are now created the way the console creates them, with optional new local storage, a choice of CPU line and the
same checks on sizes and settings. Built on the `meshive` Python SDK 1.1. Tool names and existing arguments are
unchanged.

### New

- **`cpus` tool:** the CPU lines you can rent, with prices per core and per GiB of RAM, and the `cpu_line` key to
  create a pod on one.
- **Local storage with a pod:** `estimate_pod` and `create_pod` take `local_volumes` to create local storage on the
  pod's machine (at least 10 GiB, not for spot pods). The estimate includes its hourly price.
- **CPU line:** `estimate_pod` and `create_pod` take `cpu_line` for CPU pods. Without it the cheapest line that fits
  is used.
- **One operation by ID:** `transactions` takes `transaction_id`, such as the one `create_pod` returns, and shows how
  that operation ended, including failures that have already dropped off the list.
- **Waiting starts:** `start_pod` takes `placement` `wait_same_node` or `wait_any_node` to wait for free capacity
  and start the pod as soon as there is room.
- **Template ports:** `estimate_pod` and `create_pod` take `template_ports` to rename, renumber, expose or hide the
  ports the template opens, like the console's deploy screen. Template ports can't be removed; `ports` opens new ones.
- **Storage usage warning:** `create_storage` takes `usage_warning` and `usage_warning_threshold` (0-1, default 0.9)
  to warn when the volume fills up.

### Changed

- `start_pod` without `placement` lets the server pick what the console's Start button would, instead of always
  `same_node`, and the result's `placement` says which one it used. If that pick would move a pod with unpreserved
  workspace files, the call returns `data_loss_consent_required` with the placement to retry with after consent;
  `allow_data_loss` is only sent together with an explicit `placement`.
- `start_pod` with `wait_any_node` asks for the same separate consent to lose unpreserved workspace files as
  `any_node`, since both can move the pod to another machine.
- Pod settings are checked like the console: vCPU and RAM must be sizes the console offers, ports must not repeat the
  template's, required template variables need values and the start command is limited to 4,096 characters. Errors
  say what to change, with the allowed values where there are some.
- CPU pods default to 8 cores and 32 GiB of RAM, like the console (before: 2 vCPU and 4 GiB).
- `create_storage` needs at least 10 GiB, like the console.
- `volumes` attaches existing network storage only, and `create_storage` creates network storage only. Create local
  storage together with a pod through `local_volumes`.
- `region` (pods and storage) and `disk_type` (storage) are still accepted but ignored.

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
