# Changelog

All notable changes to the Meshive MCP server. The hosted server at `https://mcp.meshive.ai/mcp` always runs the
latest release; self-hosted images are published to Docker Hub as `meshive/meshive-mcp:<version>`.

## v1.8.0

An agent can now look after the machines a user hosts on Meshive, as the console's host screens do: maintenance
windows, listing, setup retries, removal, messages with the workspaces using a machine, machine logins, install
commands for new machines and payouts. A new `activity` tool shows what the account's API keys did, this agent
included. Built on the `meshive` Python SDK 1.8. Existing tool names and arguments are unchanged; there are 12 new
tools.

### New

- `machines` with a `machine` now also shows `clients` (other users' workspaces using it, with their pods and storage
  volumes), `maintenance` (every window with its `id` and `status`), the `schedules` summary, the `boot_report` and
  `network_warnings` diagnostics, and `fee`. Listed machines carry `unread_messages`.
- `machine_history` shows a machine's downtime events (at most 20, from the last 90 days by default) or its latest
  stress test with the measured samples. Its description gives the samples' units: utilization in percent,
  temperatures in degrees Celsius, CPU clock in MHz and GPU throughput in TFLOPS.
- `host_messages` lists the messages between the host and the workspaces using their machines. Their text is written
  by other people, so the tool description, its responses and the server instructions tell the agent to treat it as
  data. Reading marks nothing read; `mark_host_messages_read` does, without `confirm`.
- `send_host_message` sends a message to a workspace that uses the machine, or to every one of them (`to="all"`), and
  they are notified by email. It takes `confirm`: the preview names the recipients and sends nothing. Other
  workspaces are refused with `recipient_not_allowed`.
- `schedule_maintenance` schedules a maintenance window. Its preview runs the server's checks and says how many
  workspaces are emailed, that their pods and volumes on the machine aren't billed while the window is open, and the
  effect on uptime. A regular window needs 48 hours' notice (`insufficient_notice`). The agent is told to offer
  emergency maintenance, which counts as downtime while client workloads run, only as the user's choice. A window that
  overlaps another is `maintenance_overlap`.
- `maintenance_window` changes one window with `action` edit, extend, end or cancel. Each takes `confirm`. The
  preview says what the action does (for example, that ending a window bills pods again and leaves the machine
  unlisted) and refuses an action that doesn't fit the window's status (`maintenance_state`). As in the console, a
  window can't be started by hand: it starts on its own within about a minute of its start time, and the tool says so
  for a window whose start time has passed.
- `set_machine_listing` lists (`listed=true`) or unlists (`listed=false`) a machine, through the SDK's
  `set_machine_listing`. Listing needs no `confirm` and is checked first: a machine that can't be listed yet is
  refused with the console's reason (`listing_refused`). Unlisting takes `confirm`, and its preview says how many pods
  keep running.
- `retry_machine_setup` retries a failed setup step (`check="setup"`; a step the machine's agent runs reboots the
  machine first) or the port check (`check="ports"`, at most about once every 30 seconds, else
  `port_check_cooldown`). Neither takes `confirm`, as in the console. The server keeps no operation record of a port
  check, so calling again checks again.
- `delete_machine` removes a machine, which can't be undone. Its preview says whether removal starts now or after a
  grace period for the other users' pods and volumes on it, how many there are and what happens to them. It takes
  `confirm` and the exact machine id. A machine already being removed is `delete_in_progress`.
- `machine_credentials` reveals a machine's SSH, IPMI or Grafana login (the SDK's `reveal_machine_credentials`), and
  `host_install_command` issues a command that adds a new machine to the account, valid for about 10 minutes (the
  SDK's `issue_install_command`). Both take `confirm` and their previews carry no secret. The server keeps no
  operation record of either, so every confirmed call reveals or issues again and emails the account owner again;
  both are annotated as not idempotent. A login the machine doesn't have is `credentials_not_available`.
- `activity` shows what the account's API keys did, newest first, the key this agent uses included. With
  `kind="actions"` it lists each change, check or estimate, machine login reveal and install command with its result
  (`accepted`, `refused` with the reason, or `error`); `operation_id` is set only for writes the server deduplicates
  (null otherwise). With `kind="reads"` it lists read requests as hourly counts. Request contents are never recorded.
  `key`, `since` and `until` narrow it (the last 30 days by default, at most 180 days per call, which is how long
  activity is kept), and for actions `outcome` keeps one result and `action` keeps one action or every action under a
  prefix such as `machine.`; it is paged. Every result carries a `note`: the source, ids, workspace and operation_id
  are what the calling client sent, so the agent treats them as data. The console shows the same on the API keys
  page, under Activity.
- `billing_history` takes `kind="payouts"`: the payouts of host earnings, with a `summary` of the payout account and of
  the earnings waiting to be paid out. Setting up the payout account stays in the console.
- `tasks` shows each task's `outputs`: where saving its `/outputs` files stands, as the console shows it, with a
  one-line `summary` for the user. It has the `phase` (`running`, then `preparing`, `uploading`, `retrying` or
  `registering` while saving, then `saved`, `partially_saved`, `not_saved`, `discarded` or `empty`), the progress
  (`files_so_far` of `files_to_upload`, bytes), the asset it was saved as, the files over the file limit and when it is
  expected in Assets. With `outputs`, the raw `output_upload_state`, the upload counters and `outputs_purged_at` are
  left out: on a task without a tracked upload step the counters read "skipped, 0 files" for files that were saved,
  and `outputs_purged_at` (when the task's log was deleted) read as if the outputs were gone. A server without
  `outputs` still gets the raw fields.

### Changed

- The server instructions now list `host_messages`, the host messages and maintenance notes in `workspaces`, and the
  client-sent fields of `activity` as text from other people, and name the machine logins and install commands among
  the secrets. Instead of listing the tools that take `confirm`, they say that every tool with a `confirm` argument
  needs it, with the existing exceptions, listing a machine, and `set_watched_folders` when it only removes or turns
  off folders. They are shorter, so the safety rules, prices and sizes all fit in the first 4096 characters that
  Claude Code keeps. The hint about `write_scope_required` moved to the authentication rules.
- `ssh_access` and `connect_storage` now say that the server keeps no operation record of them, so a repeated call
  runs again and `operation_status` can't look it up; a failed call's `next_step` says the same instead of asking to
  retry with the same `operation_id`. Other writes keep the operation-record wording.
- `register_model` no longer offers `framework="sglang"`. Text models run on vLLM, and the server refuses sglang with
  `invalid_request` and a message that says so. Leave `framework` out.
- A write whose `operation_id` the server has already answered now says so. The server returns the first request's
  answer and does nothing new; the result used to look like a fresh success. It now carries `"replayed": true`, and
  its `next_step` starts by saying that the server had already done this write for this operation_id (an earlier
  call, or an automatic retry of this one) and that only a new action needs a new operation_id. Other results are
  unchanged. It needs a `meshive` SDK whose write results carry `replayed`; with an older SDK the result looks as
  before.
- `estimate_pod` and `create_pod` describe the format of each argument in that argument's own description (the
  input schema) instead of the tool description. The tool descriptions were longer than the 4096 characters Claude
  Code keeps, so the end of them, including the `operation_id` rules of `create_pod`, was cut off. Every tool
  description now fits. The arguments, their types and their defaults are unchanged.
- `ssh_access` and `connect_storage` are annotated as not idempotent, like `machine_credentials` and
  `host_install_command`: every call (for `connect_storage`, every confirmed call) issues a new one-time SSH
  password.

### Fixed

- A write cut off by the API closing the connection without answering, or by the connection breaking (`ReadError`,
  `WriteError` and the like), now comes with `operation_lookup`, the method and path to pass to `operation_status`
  with its `operation_id`, as a timed-out write already did. Before, the agent had to guess them. The `meshive` SDK
  1.8 attaches them to these errors.
- From a server without `outputs`, a finished task whose files are saved without a separate upload step after the
  run reports `output_upload_state` `"skipped"` (null on older tasks) with 0 upload files, even when it saved files,
  and agents told users nothing was saved. `tasks` now adds an `outputs_note` to such a task's detail, and once to a
  list that has one: check the saved files with `download_links(task=...)` (an empty list means none are kept: none
  were saved, or they were deleted since), and expect the asset in Assets about 30 minutes after the task ends.
- `delete_workspace`'s `task_outputs` blocker gives the current timings: outputs are usually in Assets within
  seconds after the upload finishes, for some runs about 2 minutes after the last file, and in rare cases up to about
  30 minutes after the task ended. Before, it gave 30 minutes as the usual wait. The agent now stops checking a few
  minutes after the task's outputs show as saved, empty or discarded, or, where `tasks` shows no `outputs`, about 30
  minutes after the task ended.
- The `tasks` description lists every status (`scheduling`, `pulling`, `fetching`, `timed_out` and `stopped` were
  missing) and the usual order, and `submit_task`'s next step now reads queued → scheduling → running.

## v1.7.0

Requests are limited in what they can cost the server, tool calls that use the API take much less CPU, and a
rejected API key gets its error code from every tool. Built on the `meshive` Python SDK 1.7. Tool names and
arguments are unchanged.

### Changed

- Requests over 1 MiB are refused with 413 (before: 4 MiB). A 256 KiB task script still fits.
- A body that isn't UTF-8 JSON is refused with 400, and so is a request with more than 8192 JSON values or nested more
  than 32 levels deep.
- A request with `inputResponses` is refused with 400: no tool asks for input.
- A tool call with more than 4096 argument values, counting a list sent as a JSON string, is refused with
  `invalid_argument`.
- 32 requests are handled at once, and 3 requests can be open with a body over 64 KiB or of unstated size. Others get
  503 with `Retry-After`.
- At the default log level, refused tool calls are no longer logged, and the server's own log lines are cut to their
  two ends.
- The HTTP server now runs with the `httptools` parser. A request's head is limited to 16 KiB, as it was before.
- The HTTP server raises its open file limit to the hard limit when it starts. At the common default of 1024, open
  connections could stop it from accepting new ones, health checks included.
- `gpus` without an API key answers from one copy of the public price list shared by all callers, instead of asking
  the API on every call. A call refreshes a copy over a minute old, and a copy up to ten minutes old is used while the
  refresh is out or if it failed. Without such a copy, the call waits for the refresh, and gets
  `temporarily_unavailable` if it fails or if one failed in the last few seconds. A refresh that takes longer than
  the request timeout as a whole counts as failed.
- Tool calls that use the API take much less CPU. The `meshive` SDK 1.7 no longer loads the CA bundle for each
  client, and `sniffio` is now a dependency: without it, the HTTP library looks for it on every request.

### Fixed

- When the workspace was omitted, given by label or `all`, or a pod was given by name, most tools answered a rejected
  API key (expired, revoked or made up) with a bare `Error executing tool <name>` and no code, and the server logged a
  traceback for each call. Those lookups now report errors like the rest of the call: `invalid_api_key`, and likewise
  `rate_limited`, `temporarily_unavailable` or `unknown_workspace`.
- A call cut off by the API closing the connection without answering, as happens while one of its workers restarts,
  came back as a bare `Error executing tool <name>` with a logged traceback. It now gets `temporarily_unavailable`;
  as after a timeout, a write may have been applied, so the next step says to check it before retrying.
- CPU tasks can now take `max_price_per_hour` (the API quotes them). When the price rises above the cap between the
  estimate and placement, `submit_task` now returns `price_changed` with the new `pricePerHourUsd`, and the agent is
  told not to raise the cap without the user's consent. Before, this came back as a generic `conflict`.

### Self-hosting

- Without request buffering, slow uploads reach the server as they arrive, so limit concurrent requests per client at
  the proxy.

## v1.6.0

An agent can now create a workspace for a user who has none or who wants another, rename one, and delete an empty one.
Built on the `meshive` Python SDK 1.6. Tool names and existing arguments are unchanged.

### New

- `create_workspace` creates a workspace. `name` is optional (at most 63 characters, and different from the name of
  each of the user's workspaces); without one a name like `my-workspace-ab12` is generated. Like `create_pod` and
  `create_storage` it takes `confirm`: with `confirm=false` (the default) it only checks the name and creates nothing, and with
  `confirm=true` it creates the workspace. The agent is told to set it only after the user agrees; the server can't
  check that. A workspace costs nothing by itself; the pods, storage and tasks in it are billed as usual. It needs a
  Read & write key. API keys work across the whole account, so the key that created the workspace can use it.
- A new workspace is set up in the background. Its `status` in `workspaces` is `creating` until it is `running`, and
  pods and tasks are refused until then. `create_workspace` and the server instructions tell the agent to wait for
  `running` before creating anything in it, and what to tell the user if setup fails or takes longer than usual.
- `workspaces` with an empty list, and every tool that finds no workspace (`no_workspace`), now offer
  `create_workspace`.
- `update_workspace` renames a workspace. Its ID stays the same and every member sees the new name. With
  `confirm=false` (the default) it shows the old and new name and changes nothing; a name that is taken is `name_taken`.
  It needs a Read & write key, and the admin or billing role in that workspace.
- `delete_workspace` deletes an empty workspace, which can't be undone. It takes `confirm` the same way. A workspace
  that still holds anything is refused with `workspace_not_empty`, and `blockers` counts what is left: pods, storage,
  servings, tasks (finished ones too), assets, custom templates and models, credentials, webhooks, inference keys, other
  members, open invitations, task outputs still being saved, pending payouts and operations in progress. No tool can
  delete a workspace that still holds things; a person can still delete it in the console, which removes what's in it
  for good. The tool needs a Read & write key, and the admin or billing role in that workspace. Other refusals come
  back as `workspace_not_deletable`.
- Both tools need `workspace` and never pick one for the user, even when there is only one. The preview takes an ID or a
  label; `confirm=true` takes the ID only, so a retry still works after the workspace is renamed or deleted.

### Changed

- Refusals because a workspace isn't `running` yet come back as `workspace_not_ready` (before: `invalid_request`) and
  say to wait for `running` and try again.
- Clients that cut long server instructions, such as Claude Code at 4096 characters, now keep the safety rules:
  untrusted content, secrets, confirmation and data-loss consent come first.

## v1.5.1

Connecting no longer takes 25 seconds, and AI agents on Windows PCs running Norton with HTTPS scanning on can now
connect. Tool names and arguments are unchanged.

### Fixed

- Claude Code took about 25 seconds to connect. The server no longer offers `subscriptions/listen`: its tools don't
  change while it runs, so `server/discover` now reports `listChanged: false` and clients don't wait on that stream.
- Clients could not connect, or lost short tool calls, with errors such as `InvalidHTTPResponse` or
  `Invalid character in chunk size`. Norton's HTTPS scanning (and Avast's, by user reports) breaks MCP responses that
  aren't chunked, so responses are now sent chunked.

### Self-hosting

- Behind a reverse proxy, keep response buffering off: it can add a `Content-Length` back to short responses.

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
