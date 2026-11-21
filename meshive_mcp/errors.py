"""Exceptions → instructions for the model.

In MCP, the model reads an `isError: true` response and picks its next action itself. So instead of a stack trace
or the raw server text, it gets one JSON object saying **what went wrong (code, message) and what to do next (next_step)**.
Always say in words whether to retry — if a 429 is passed through as-is,
the model retries immediately and makes things worse.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from meshive.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    EndpointLockUnavailableError,
    InsufficientCreditError,
    MeshiveAPIError,
    MeshiveError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    WaitTimeoutError,
)

try:    # SDK 1.2: connect_storage found the storage in phase "error"
    from meshive.exceptions import StorageConnectionError
except ImportError:     # an SDK without it reports the failure as a phase in the result instead
    StorageConnectionError = None  # type: ignore[assignment,misc]

CONSOLE_URL = "https://console.meshive.ai"

KEYS_PAGE = f"{CONSOLE_URL}, Account > API keys"
# How to swap keys — keys expire (write keys default to 30 days), and Claude Code's `mcp add` rejects a name that already exists.
REPLACE_KEY = ("replace the old key in this MCP server's configuration (in Claude Code, run "
               "`claude mcp remove meshive` first, then add it again)")

NEXT_STEP_NO_KEY = (
    f"Ask the user to create a Meshive API key in the console ({KEYS_PAGE}) and put it in this MCP server's "
    "Authorization header as 'Bearer meshive_...'. Do not retry until they confirm it is set."
)
NEXT_STEP_INVALID_KEY = (
    "The configured API key was rejected (invalid, expired, revoked, or the account is inactive). "
    f"Ask the user to check the key, or create a new one in the console ({KEYS_PAGE}) and {REPLACE_KEY}. "
    "Do not retry."
)
NEXT_STEP_WRITE_SCOPE = (
    "This action needs an API key with the 'write' scope. Ask the user to create a Read & write key in the console "
    f"({KEYS_PAGE}) and {REPLACE_KEY}. Do not retry with the current key."
)
NEXT_STEP_FORBIDDEN = (
    "The user's account is not allowed to do this (for example, not an admin of the workspace). "
    "Tell the user and do not retry."
)
NEXT_STEP_NOT_FOUND = (
    "Verify the identifier with the matching list tool (pods, storages, templates, ...). "
    "Do not guess identifiers."
)
# The write half of NEXT_STEP_UNAVAILABLE. A write the server keeps no operation record of has nothing for
# operation_status to find, so the tool wrapper takes this sentence out and says that a retry runs the call again.
UNAVAILABLE_WRITE_STEP = ("For a write, check operation_status first; reuse the original operation_id and arguments if "
                          "retrying. Never create a new operation for an unknown outcome. ")
NEXT_STEP_UNAVAILABLE = ("Wait {retry_after}s. For a read, retry once. " + UNAVAILABLE_WRITE_STEP +
                         "If calls keep failing, the account tool's platform_notices shows whether Meshive announced an "
                         "incident.")
# delete_workspace, on its preview and on the server's own refusal. A key can only delete an empty workspace, on purpose.
NEXT_STEP_WORKSPACE_NOT_EMPTY = (
    "Nothing was deleted. Tell the user what `blockers` says is left, and clear nothing on your own. Pods, storage, "
    "servings and custom models can be deleted with their tools, assets with delete_asset, custom templates with "
    "delete_template, running tasks stopped with stop_task and finished ones deleted with delete_tasks, but only if "
    "the user asks. The same goes for saved credentials (delete_credential), webhooks (delete_webhook), inference keys "
    "(revoke_inference_key: an admin can revoke every key of the workspace, inference_keys lists them), and members "
    "and open invitations (remove_member). `pods` also counts serving replicas, which go with "
    "their serving, and "
    "system pods, which go with their storage volume (delete_storage); the pods tool lists those with "
    "include_system=true. A volume the storages tool still shows stays and bills until it is deleted; only a deleted "
    "pod's hidden temporary volume can stay under `storages` for a few minutes and clear by itself. "
    "`operations_in_progress` and `task_outputs` clear on their own. `operations_in_progress` usually clears within "
    "minutes, at most about 30 minutes, so check again shortly with delete_workspace without confirm. `task_outputs` "
    "clears once the outputs are in Assets: usually within seconds after the upload finishes, for some runs about 2 "
    "minutes after the last file, and in rare cases up to about 30 minutes after the task ended; a task that wrote no "
    "files can count for a short while after it ends. A failed upload attempt is retried automatically, and the tasks "
    "tool shows how far saving has got (`outputs`) for tasks still in the list. If it is still listed a few minutes "
    "after the task's outputs show as saved, empty or discarded (where the tasks tool shows no `outputs`, about 30 "
    "minutes after the task ended), stop checking and tell the user to look at the task in the console or contact "
    "support. `pending_payouts` clears once "
    "the earnings are paid out, usually within a day; earnings on hold, or a failed payout, can take longer, so tell "
    "the user to contact support. Deleting in the console before that loses them. Never suggest a way around this "
    "check. No tool can delete a workspace that still holds things. A person can still delete it in the console, which "
    "removes what's in it for good."
)


# --- Port locks and access keys ---
# A pod create, edit or estimate asked for a locked port and the server can't lock ports. The SDK sends nothing with the
# ports open instead, and neither may the agent: an open port is the user's decision.
_LOCK_UNAVAILABLE = {
    "disabled": ("Ports can't be locked on Meshive right now: port locking is turned off on the platform.",
                 "right now"),
    "unsupported": ("This Meshive API server doesn't support locking ports yet.", "on this server yet"),
}
# Added to the message of a pod request refused by a server without locks: what sending it would have done.
_WOULD_EXPOSE = " It would expose them to anyone who knows the URL."
_NEXT_STEP_LOCK_UNAVAILABLE = (
    "Nothing was created or changed, and the request was not sent again with open ports. Tell the user that the ports "
    "this request exposes over HTTPS can't be locked {when}. Never go ahead with open ports on your own. The user can "
    "wait and try again later with the same arguments, keep the port internal (\"external\": false on its item in "
    "`ports` or `template_ports`), or decide to have it open to anyone who knows the URL. Only if the user explicitly "
    "chooses open ports, call the tool again with lock_public_ports=false (or \"locked\": false on that port's item), "
    "without confirm first, so that the preview shows them which ports would be open.")
# set_port_lock, create_access_key and roll_access_key while the platform has locking turned off, and an edit that only
# locks ports the pod already exposes: nothing here could go ahead with an open port instead.
_NEXT_STEP_LOCK_OFF = (
    "Nothing was changed: every port keeps the lock it has now. Port locking is turned off on the platform right now, "
    "so a port can't be locked and access keys can't be created or rolled; unlocking a port and listing or revoking "
    "keys still work. Tell the user and try again later; don't retry in a loop.")
# The same calls on an API server that has no port locks yet.
_NEXT_STEP_LOCK_UNSUPPORTED = ("Nothing was changed. This Meshive API server has no port locks and access keys yet. "
                               "Tell the user; don't retry.")
NEXT_STEP_PORT_NOT_FOUND = "Use a port number from the pod's `endpoints` in the pods tool. Do not guess."
NEXT_STEP_PORT_NOT_PUBLIC = (
    "Nothing was changed. Only a port exposed over HTTPS has a lock and access keys; the pods tool shows each port's "
    "`access`, and this one is internal (reachable only inside the workspace) or belongs to a platform-managed "
    "workload. To expose a port, the user edits the pod (update_pod `template_ports` with \"external\": true, which "
    "restarts a running pod); it is then locked by default.")
NEXT_STEP_PORT_NOT_LOCKED = (
    "Nothing was created or changed. An access key can be created or rolled only while the port is locked; this one "
    "is unlocked, so anyone who knows its URL gets in without a key. Ask the user whether to lock it (set_port_lock "
    "with locked=true; no restart), then call again.")


def lock_unavailable(reason: str, *, pod_request: bool) -> ToolError:
    """`pod_request`: the refusal of a pod create, edit or estimate, which could go ahead with open ports if the user
    chose that. Otherwise the refusal of a lock or access key call, which has no such way out."""
    reason = reason if reason in _LOCK_UNAVAILABLE else "disabled"
    message, when = _LOCK_UNAVAILABLE[reason]
    if pod_request:
        next_step = _NEXT_STEP_LOCK_UNAVAILABLE.format(when=when)
        message += _WOULD_EXPOSE if reason == "unsupported" else ""
    else:
        next_step = _NEXT_STEP_LOCK_OFF if reason == "disabled" else _NEXT_STEP_LOCK_UNSUPPORTED
    return tool_error("endpoint_lock_unavailable", message, next_step, reason=reason)


def lock_unavailable_reason(error: ToolError) -> str | None:
    """The `reason` of an endpoint_lock_unavailable error, or None for any other error."""
    try:
        body = json.loads(str(error))
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("code") != "endpoint_lock_unavailable":
        return None
    return body.get("reason") or "disabled"


def tool_error(code: str, message: str, next_step: str, **extra: Any) -> ToolError:
    body = {"code": code, "message": message, "next_step": next_step, **extra}
    return ToolError(json.dumps(body, ensure_ascii=False))


def invalid_argument(message: str, **extra: Any) -> ToolError:
    return tool_error("invalid_argument", message,
                      "Fix the argument and call the tool again.", **extra)


# The kind of 409 is told apart by the server's detail.title. Extra info (availability, pricePerHourUsd,
# linkedPods, available, resourceBreakdownComplete) is carried in detail as-is and passed on to the model.
_CONFLICT_CODES = {
    "operation outcome unknown": ("operation_outcome_unknown", "Check operation_status and the recorded task/transaction. "
                                   "The request will not execute again; pending/unknown records require reconciliation."),
    "price unavailable": ("price_unavailable", "The server cannot quote this capped request. Keep the user's cap; "
                           "do not remove or increase it without their explicit consent."),
    # start_pod: the placement (the one sent, or the one the server picked when none was sent) moves a pod that has
    # a scratch disk. Nothing was started; detail.placement names the move to consent to.
    "data loss consent required": ("data_loss_consent_required",
                                   "Nothing was started. Starting this pod with the `placement` in this error moves it "
                                   "to another machine and permanently deletes its scratch disk. Show "
                                   "the user that warning for this pod and placement and get separate explicit consent "
                                   "to the loss. Only then call start_pod again with placement set to that `placement`, "
                                   "allow_data_loss=true, confirm=true and the same operation_id. Without that consent "
                                   "don't retry with allow_data_loss."),
    "no capacity": ("no_capacity", "Nothing is available for this request right now. Show the `available`/`availability` "
                                   "details to the user and suggest a smaller request, a different GPU, or trying later. "
                                   "Do not retry blindly."),
    # A 409 after retrying a create whose response was lost is common — only saying "use another name" makes the model create one more resource.
    "name taken": ("name_taken", "A resource with this name already exists. If you just tried to create it and did not "
                                 "get a clear answer, that is probably it — check the matching list tool before creating "
                                 "anything else. Otherwise pick another name or use the existing one. For a rename "
                                 "(update_pod, update_storage, update_workspace or save_template `name`), ask the user "
                                 "for another "
                                 "name. A resource being deleted still holds its name until deletion finishes."),
    "price exceeds cap": ("price_exceeds_cap", "The estimate is above the user's price cap. Show `pricePerHourUsd` and ask "
                                               "whether to raise the cap or choose cheaper hardware."),
    # submit_task (CPU): between the estimate and placement the cheapest machine went away or got pricier, and what is
    # left is above the cap. The server's message says "submit again" (the console shows the new price first).
    "price changed": ("price_changed", "Nothing was created: the price went above the user's cap before placement. Show "
                                       "`pricePerHourUsd` and ask whether to raise the cap or choose other hardware. "
                                       "Do not raise or remove the cap without their explicit consent."),
    "storage in use": ("storage_in_use", "The storage is mounted by the pods in `linkedPods`. Stop/delete or detach them "
                                         "first (ask the user)."),
    "workspace not empty": ("workspace_not_empty", NEXT_STEP_WORKSPACE_NOT_EMPTY),
    "request in progress": ("in_progress", "The same request is still being processed. Wait a few seconds, then check the "
                                           "matching list tool instead of resubmitting."),
    "vram tier required": ("vram_tier_required", "Pass `gpu_vram_gb` — this GPU model comes in several VRAM tiers "
                                                 "(see `available`)."),
    # start_pod: nothing can take the pod right now (same_node or any_node).
    "no available node": ("no_available_node", "No machine can take the pod right now. Ask the user whether to wait: "
                                               "placement wait_same_node waits for its original machine, wait_any_node "
                                               "for any machine (it needs the same data-loss consent as any_node). "
                                               "Otherwise try again later."),
    # start_pod same_node/wait_same_node when the saved hardware no longer matches the original machine.
    "original node can't run this pod": ("same_node_spec_mismatch",
                                         "Waiting for its original machine won't help. Offer placement any_node or "
                                         "wait_any_node instead; start_pod's preview says whether moving needs "
                                         "data-loss consent."),
    # update_pod: the pod's machine can't fit the edit. Nothing changed; moving it needs the user's agreement.
    "node migration required": ("node_migration_required",
                                "Nothing was changed. The pod's machine can't fit this change, so the pod would have to "
                                "move to another machine. "
                                "Tell the user why and ask whether to move it. Only if they agree, call update_pod again "
                                "with allow_node_migration=true, confirm=true and the same operation_id; its preview "
                                "says whether the move deletes a scratch disk and needs separate consent "
                                "(allow_data_loss=true). Otherwise suggest a smaller change."),
    # update_pod: attached local storage keeps the pod on its machine, and that machine can't fit the edit.
    "cannot change resources": ("cannot_change_resources",
                                "Nothing was changed. The pod has local storage attached, which keeps it on its machine, "
                                "and that machine can't fit the change. Ask the user whether to detach the local storage "
                                "(update_pod `detach_storages`; the storage and its data stay, but the pod can no longer "
                                "use them) or to keep the current resources. Then preview the edit again."),
    # update_pod on an older pod whose RAM size can't be read back exactly.
    "resource values need confirmation": ("resource_values_need_confirmation",
                                          "This older pod's RAM size can't be read back exactly. Ask the user which RAM "
                                          "size to use, pass it as `ram_gb` together with the change and preview the "
                                          "edit again."),
    # update_pod while the pod is starting, stopping or already being edited.
    "operation in progress": ("operation_in_progress",
                              "Another operation on this pod (starting, stopping or an earlier edit) is still running, "
                              "so nothing was changed. Wait until the pods tool shows the pod running or stopped, then "
                              "try again."),
    # update_pod add_input_assets / remove_input_assets: an asset was added to or removed from the pod meanwhile.
    "asset configuration changed": ("asset_config_conflict", "Something changed the pod's assets meanwhile; preview "
                                                             "again."),
    # --- hosting ---
    # schedule_maintenance / maintenance_window edit or extend: the times run into another window of the machine.
    "overlapping maintenance window": ("maintenance_overlap",
                                       "Nothing was scheduled or changed. Another maintenance window of this machine "
                                       "overlaps these times; the message names it. Ask the user for other times, or "
                                       "whether to move or cancel that window first (maintenance_window)."),
    # The machine is being removed, still being set up, or getting a platform software update; the message says which.
    "maintenance not available": ("maintenance_not_available",
                                  "This machine can't go into maintenance right now, and nothing was changed. Tell the "
                                  "user the reason in the message. A software update ends within minutes, so after one "
                                  "you can try again shortly; otherwise don't retry."),
    # send_host_message from a workspace, by `pod`: the pod has no machine yet.
    "pod not placed": ("pod_not_placed", "Wait until the pod is placed, then try again."),
    "not retryable": ("not_retryable", "Only a failed setup step can be retried, and the machine's current step isn't "
                                       "failed. Check its state with the machines tool before retrying."),
    # machines include_fan: live fan readings need the machine online.
    "machine offline": ("machine_offline", "The machine is offline; live readings come back when it reconnects. Check "
                                           "its state with the machines tool."),
}

_MAINTENANCE_STATE = ("Nothing was changed: the maintenance window or the machine isn't in a state that allows this (the "
                      "message says why). Check the window's `status` in the machines tool (a machine's "
                      "`maintenance`): edit or cancel a window that hasn't started, extend or end one in progress. A "
                      "window starts by itself within about a minute of its start time. Then use the action that fits, "
                      "if the user still wants it.")
# set_machine_listing, on the server's refusal and on its own check before listing.
NEXT_STEP_LISTING_REFUSED = ("Nothing was changed. Tell the user the reason in the message: it usually needs a fix on the "
                             "machine or in the console first (prices, hardware, ports, boot or the machine's state). "
                             "Try again only after that is fixed.")

# update_pod's own data-loss refusal carries no placement (start_pod's does): the move would come from
# allow_node_migration.
_DATA_LOSS_EDIT_STEP = ("Nothing was changed. If this edit moves the pod to another machine (allow_node_migration), its "
                        "scratch disk is permanently deleted. Show the user that warning for this pod and "
                        "get separate explicit consent to the loss. Only then call update_pod again with the same "
                        "arguments plus allow_data_loss=true, confirm=true and the same operation_id. Without that "
                        "consent, call it without allow_node_migration instead.")

_WORKSPACE_NOT_READY = ("No pod or task was created. Call the `workspaces` tool with no arguments and wait until this "
                        "workspace's status is `running` (a new one stays `creating` while it is set up), then try "
                        "again. If the workspace isn't listed or doesn't reach `running`, tell the user.")

# The kind of 400 is told apart by detail.title too. Extra info (allowed, keys, port, name, maxLength, templatePorts,
# attached, paths, currentSizeGb, failed, available) is passed on.
_BAD_REQUEST_CODES = {
    # The operation_id was already used for a request with other arguments, which may have run. Neither "fix the
    # arguments" nor "retry with the same operation_id" helps: that earlier operation has to be looked up first.
    "idempotency-key reused": ("operation_id_reused",
                               "Nothing new was done: this operation_id was already used for a request with different "
                               "arguments, which may have run. Call operation_status with this operation_id and "
                               "operation_lookup and tell the user what it shows before doing anything else. Don't "
                               "change the arguments to get past this. Only if the user wants this different action "
                               "too, call again with a new operation_id."),
    # A confirmed create (pod, storage, workspace) when nothing can take it now, e.g. the machine filled up after the
    # estimate. The arguments were valid, so fixing them is not the advice.
    "no available cluster": ("no_capacity",
                             "Nothing was created: no machine can take this right now (what the estimate showed may "
                             "have been taken meanwhile). Estimate again and suggest other hardware or a smaller "
                             "request, or trying later. Do not resend the same request blindly."),
    # download_links on an asset linked from Hugging Face, CivitAI or a URL: Meshive stores none of its files.
    "linked asset not downloadable": ("linked_asset_not_downloadable",
                                      "This asset is linked from its source (its `source` in the assets tool), so "
                                      "Meshive keeps no copy to link to. Download the files from that source instead; "
                                      "don't retry download_links for it."),
    "local storage not attachable": ("local_storage_not_attachable",
                                     "That storage is local to one machine, and a new pod can only attach network "
                                     "storage. Attach network storage in `volumes`, or create new local storage with the "
                                     "pod in `local_volumes`; ask the user which they want."),
    "local storage not supported": ("local_storage_not_supported",
                                    "create_storage makes network storage only. Use storage_type \"nfs\", or create "
                                    "local storage together with a pod through create_pod `local_volumes`."),
    "local volume not allowed": ("local_volume_not_allowed",
                                 "Spot pods can't have local storage. Drop `local_volumes` and use network storage in "
                                 "`volumes`, or use rental_type \"demand\"; ask the user which. To make an existing pod "
                                 "spot, detach its local storage in the same update_pod call (`detach_storages`)."),
    "command too long": ("command_too_long",
                         "`command` is limited to `maxLength` characters. Put the long script in a file (in the image, "
                         "on storage or in an input asset) and make `command` run that file."),
    "cpu line not for gpu pods": ("cpu_line_not_for_gpu_pods",
                                  "`cpu_line` is for CPU pods only. Omit it when passing `gpu_model`."),
    "vcpu not offered": ("vcpu_not_offered", "Use one of the `allowed` values for `vcpu` (the sizes the console offers "
                                             "for this hardware), or omit it for the default, then estimate again."),
    "ram not offered": ("ram_not_offered", "Use one of the `allowed` values for `ram_gb` (the sizes the console offers "
                                           "for this hardware and vCPU count), or omit it for the default, then "
                                           "estimate again."),
    "port in use": ("port_in_use", "This `port` would be open twice (the template's ports, `ports` and "
                                   "`template_ports` together). Leave it out of `ports`, or change the template's port "
                                   "with `template_ports`; each port can be open only once."),
    "port name in use": ("port_name_in_use", "Another port (the template's or yours) already has this `name`. Give the "
                                             "port in `ports` a different name, or rename the template's port with "
                                             "`template_ports`."),
    "port not in template": ("port_not_in_template", "`template_ports` can only change ports the template opens (listed "
                                                     "in `templatePorts`), by their current number. Use `ports` to open "
                                                     "a new port; template ports can't be removed."),
    "required env missing": ("required_env_missing", "The template needs values for the env variables in `keys`. Ask "
                                                     "the user for them (never invent passwords or secrets), pass them "
                                                     "in `env` and list secret ones in `secret_keys`."),
    "duplicate mount path": ("duplicate_mount_path", "Two volumes use the same mount_path. Give every entry of `volumes` "
                                                     "and `local_volumes` its own path."),
    # create: no machine has free disk for the new local storage and the scratch disk; the message says which.
    "not enough disk": ("not_enough_disk", "Nothing was created. Show the message to the user and ask whether to use "
                                           "smaller local_volumes or try again later."),
    "storage required": ("storage_required", "This template needs a persistent volume. Attach network storage in "
                                             "`volumes` or create local storage in `local_volumes`; ask the user which."),
    # Pods and tasks are refused while a new workspace is still `creating`; the detail route has no status, so the list is
    # where to watch it. The same title is used for a workspace being deleted, which the list no longer shows.
    "workspace not found or not ready": ("workspace_not_ready", _WORKSPACE_NOT_READY),
    "workspace is not ready": ("workspace_not_ready", _WORKSPACE_NOT_READY),
    # delete_workspace: a protected workspace, or a last-moment change the check before it couldn't see. The message says which.
    "unable to delete workspace": ("workspace_not_deletable",
                                   "Nothing was deleted. Tell the user the reason in the message. If it names something "
                                   "still in the workspace, delete_workspace without confirm shows what is left; "
                                   "otherwise don't retry."),
    # --- update_pod (console Pod Edit rules) ---
    "nothing to change": ("nothing_to_change", "Pass at least one setting to change. update_pod sends only what changes, "
                                               "so give just the arguments the user wants different."),
    "pod type locked": ("pod_type_locked", "A pod's type can't change: a GPU pod stays a GPU pod (`gpu_model`, "
                                           "`gpu_count`, `gpu_vram_gb`) and a CPU pod stays a CPU pod (`cpu_line`). "
                                           "Premiums and the image can't change either. For another type, the user "
                                           "creates a new pod; ask them."),
    "template env not removable": ("template_env_not_removable",
                                   "This variable comes from the template and can't be removed; set another value in "
                                   "`env` instead. `remove_env` removes only variables the user added."),
    "template port not removable": ("template_port_not_removable",
                                    "This port comes from the template and can't be removed; rename, renumber or hide it "
                                    "(external=false) with `template_ports` instead. `remove_ports` closes only ports the "
                                    "user added."),
    "env not found": ("env_not_found", "The pod has no variable with this name. Leave it out of `remove_env`."),
    "port not found": ("port_not_found", "The pod doesn't open this `port`. Leave it out of `remove_ports`; the pods "
                                         "tool lists the pod's ports under `endpoints`."),
    "storage not attached": ("storage_not_attached", "`detach_storages` names storage that isn't attached to this pod. "
                                                     "Use pv_names from `attached`."),
    "storage already attached": ("storage_already_attached", "That storage is already attached to this pod; leave it out "
                                                             "of `volumes`. To mount it at another path, list it in "
                                                             "`detach_storages` and `volumes` in the same call."),
    "local storage on another machine": ("local_storage_on_another_machine",
                                         "Existing local storage can only be attached on the pod's current machine, and "
                                         "this storage is on another one. Attach network storage in `volumes` or create "
                                         "new local storage in `local_volumes`; ask the user which."),
    "input asset not found": ("input_asset_not_found", "Use connection_id values of active input assets from pods "
                                                       "include_assets."),
    "template deleted": ("template_deleted", "This pod's template was deleted, so the edit needs a new one. Ask the user "
                                             "which template to use and pass its `template_id` (from the templates "
                                             "tool)."),
    "not with a new template": ("not_with_a_new_template", "`remove_env` and `remove_ports` apply to the current "
                                                           "template. With `template_id` the pod starts from the new "
                                                           "template's variables and ports, so leave them out."),
    "not a pod template": ("not_a_pod_template", "Pick a pod template from the templates tool."),
    "incompatible hardware": ("incompatible_hardware", "That template runs only on the other pod type (GPU or CPU), and "
                                                       "a pod's type can't change. Pick a template for this pod's type, "
                                                       "or the user creates a new pod; ask them."),
    "invalid pod settings": ("invalid_pod_settings", "The server rejected the combined settings; the message says "
                                                     "which. Fix that argument and preview the edit again."),
    # The server's own detail.code is harvest_roots_orphaned; detail.paths lists the folders.
    "watched folders would be removed": ("harvest_roots_orphaned",
                                         "Nothing was changed. After this edit the watched folders in `paths` would no "
                                         "longer be on any storage attached to the pod, so they would be removed. Show "
                                         "the user these paths and ask whether to remove them. Only if they agree, call "
                                         "update_pod again with the same arguments plus drop_watched_folders set to "
                                         "`paths`, confirm=true and the same operation_id. Otherwise keep the storage "
                                         "those folders are on."),
    # --- update_storage ---
    "storage cannot shrink": ("storage_cannot_shrink", "Storage can grow but not shrink; `currentSizeGb` is its current "
                                                       "size in GiB. Pass a larger `size_gb` or leave the size as it is. "
                                                       "For a smaller volume the user creates a new one and copies the "
                                                       "data."),
    # --- model sets (estimate_pod / create_pod `model_set`) ---
    "model set unavailable": ("model_set_unavailable",
                              "No pod was created and nothing is billed for it. `failed` lists the files of the model "
                              "set that couldn't be added and why; files that were added stay in Asset Hub and are "
                              "reused next time. Tell the user, then ask whether to try again later (the same arguments "
                              "and operation_id), pick another model set or create the pod without one (a new preview)."),
    "model set not for template": ("model_set_not_for_template",
                                   "`model_set` must be a slug the model_sets tool lists for this template; "
                                   "`available` lists them. Ask the user which one to use."),
    "model sets not supported": ("model_sets_not_supported",
                                 "This template has no model folders, so it takes no model set. Leave `model_set` out, "
                                 "or pick a template whose model_sets list is `selectable`."),
    "model set added automatically": ("model_set_added_automatically",
                                      "This template adds its own model set when the pod is created. Leave `model_set` "
                                      "out."),
    # --- gated model sets (Hugging Face license) — create_pod `hf_token_id` ---
    "hugging face token required": ("hf_token_required",
                                    "No pod was created. This model set is gated on Hugging Face: the user accepts its "
                                    "license on the model page (gated_sources in the model_sets tool) with their "
                                    "Hugging Face account, then pass that account's token as `hf_token_id` (an id "
                                    "from source_credentials). Tokens are added in the console."),
    "license not accepted": ("hf_license_not_accepted",
                             "No pod was created. The Hugging Face account of that token hasn't accepted the model's "
                             "license. Ask the user to accept it on the model page with that account (or pick a token "
                             "of an account that did), then call again."),
    "access not approved": ("hf_access_not_approved",
                            "No pod was created. The repo owner approves each Hugging Face account and hasn't approved "
                            "the account of that token yet. The user requests access on the model page and waits; try "
                            "again after approval."),
    "hugging face token rejected": ("hf_token_rejected",
                                    "No pod was created. Hugging Face rejected the token (expired or revoked). The user "
                                    "replaces it in the console; then pass the new token's id from source_credentials."),
    "model set needs a token": ("model_set_needs_token",
                                "This model set is gated on Hugging Face and can only be added through its Quick Deploy "
                                "template with `hf_token_id`. Leave `model_set` out and use that template."),
    # --- hosting ---
    # A regular window needs 48 hours' notice. Emergency maintenance changes the machine's uptime, so it is the user's call.
    "insufficient notice period": ("insufficient_notice",
                                   "Nothing was scheduled or changed. A regular maintenance window must start at least "
                                   "48 hours from now. Ask the user for a later start. Emergency maintenance "
                                   "(emergency=true) can start sooner, but it counts as downtime for the hours a "
                                   "client workload runs on the machine: offer it only as the user's choice and never "
                                   "switch to it on your own."),
    "maintenance in progress": ("maintenance_state", _MAINTENANCE_STATE),
    "maintenance already done": ("maintenance_state", _MAINTENANCE_STATE),
    "maintenance not started": ("maintenance_state", _MAINTENANCE_STATE),
    "not in maintenance state": ("maintenance_state", _MAINTENANCE_STATE),
    "listing failed": ("listing_refused", NEXT_STEP_LISTING_REFUSED),
    "unlisting failed": ("listing_refused", NEXT_STEP_LISTING_REFUSED),
    "not in any setup stage": ("not_retryable", "The machine isn't in a setup stage, so there is nothing to retry. "
                                                "Check its state with the machines tool."),
    # retry_machine_setup check="ports": the port check has a short cooldown (a 429 is rate_limited instead).
    "too many requests": ("port_check_cooldown", "A port check of this machine ran moments ago. Wait about 30 seconds "
                                                 "and retry once; the machines tool shows the result in `boot_report`."),
    "delete in progress": ("delete_in_progress", "This machine is already being removed: don't remove it again. The "
                                                 "machines tool shows its state."),
    "recipient not allowed": ("recipient_not_allowed",
                              "Nothing was sent. `to` must be a workspace id from the machine's `clients` in the "
                              "machines tool, or \"all\" for every workspace using the machine now. Ask the user which "
                              "one they mean."),
    # send_host_message from a workspace to the host of a machine it neither uses nor has talked to.
    "host not reachable": ("host_not_reachable",
                           "Nothing was sent. This workspace isn't using that machine and has no conversation with its "
                           "host. Use a machine_id from the workspaces tool (`host_threads` or `maintenance`), or "
                           "write by `pod`."),
    "message send failed": ("message_rejected",
                            "Nothing was sent. Fix what the message says (for example a blank or too long title or "
                            "text, or nobody using the machine for to=\"all\"), then show the user the new message "
                            "before sending it."),
    # machines include_fan, set_fan_curve.
    "not a gpu machine": ("not_gpu_machine", "Fan curves exist only for GPU machines. Don't retry."),
    "invalid fan curve": ("invalid_fan_curve",
                          "Nothing was saved. Fix the points as the message says (see `fan_curve.limits` in machines "
                          "include_fan=true) and preview again."),
    # --- port locks and access keys ---
    "not a public endpoint": ("port_not_public", NEXT_STEP_PORT_NOT_PUBLIC),
    "endpoint not locked": ("port_not_locked", NEXT_STEP_PORT_NOT_LOCKED),
    "endpoint not found": ("port_not_found", NEXT_STEP_PORT_NOT_FOUND),
    "too many keys": ("too_many_access_keys",
                      "Nothing was created. This port holds as many access keys as it can; the message has the limits, "
                      "and values that still work after a roll count too. Show the user the port's keys (access_keys) "
                      "and ask which one to revoke, or wait until the rolled values run out."),
    # update_pod: the edit asked to lock or unlock ports, and the server couldn't change the lock of those in `ports`.
    "lock change failed": ("port_lock_change_failed",
                           "The lock of the ports in `ports` could not be changed, so a port that was meant to be "
                           "locked may be open. The rest of the edit may have been accepted all the same: don't send "
                           "the edit again. Check each listed port's `access` with the pods tool, tell the user which "
                           "ports are not as asked, and set each one with set_port_lock (locking needs no confirm; "
                           "unlocking needs the user's go-ahead)."),
}

# 404 titles that aren't a wrong id.
_NOT_FOUND_CODES = {
    "credentials not available": ("credentials_not_available",
                                  "This machine has no login of that kind to reveal (for example, IPMI isn't set up or "
                                  "Grafana isn't configured for it). Tell the user; don't retry."),
    "port not found": ("port_not_found", NEXT_STEP_PORT_NOT_FOUND),
}
_LOCK_DISABLED_CODE = "endpoint_lock_disabled"

# --- Asset changes and deletion (update_asset, delete_asset, delete_asset_files) ---
# Told apart by detail.title, for these tools only: some titles ("Invalid Name", "No Files", "Too Many Files") come
# from other routes too, where this advice would be wrong.
NEXT_STEP_ASSET_IN_USE = ("Used by a pod or an unfinished task (assets `active_usage_contexts`). Stop or delete that pod "
                          "or wait for the task, then preview again.")
NEXT_STEP_ASSET_FROZEN = "Frozen by the platform; only Meshive support can release it. Don't retry."
NEXT_STEP_WHOLE_ASSET = ("Tell the user. If they want the asset gone, call delete_asset with this asset (it shows its "
                         "own preview).")
_ASSET_WRITE_CODES = {
    "asset name conflict": ("name_taken", "Another asset already has this name; it may be hidden because the catalog "
                                          "added it (check with assets include_catalog=true). Ask the user for "
                                          "another name."),
    "output type is immutable": ("asset_type_locked", "Output assets keep their type; change only the name."),
    "invalid asset type": ("invalid_argument", "Use dataset, model, adapter, checkpoint, config or file; no asset can "
                                               "be made an output."),
    "invalid name": ("invalid_argument", "Use 1-255 characters."),
    "nothing to update": ("invalid_argument", "Pass `name`, `asset_type` or both."),
    "asset not editable": ("asset_not_editable", "Merged, deleted or purged assets can't be changed."),
    "asset not deletable": ("asset_not_editable", "Merged or purged assets can't be deleted, and don't need to be."),
    "asset in use": ("asset_in_use", NEXT_STEP_ASSET_IN_USE),
    "asset frozen": ("asset_frozen", NEXT_STEP_ASSET_FROZEN),
    "upload session active": ("upload_in_progress", "A running pod is still saving files into this asset. Stop the pod "
                                                    "or wait until its upload ends, then preview again."),
    "whole collection delete required": ("whole_asset_delete_required", NEXT_STEP_WHOLE_ASSET),
    "selection changed": ("selection_changed", "The files matching this selection changed since the preview. Preview "
                                               "again (new operation_id)."),
    "too many files": ("too_many_files", "At most 1000 files per call: split `file_ids` or narrow `paths`."),
    "no files": ("no_files_matched", "Nothing matched; check the paths with the assets tool."),
    # --- export_asset, buckets ---
    "already in your bucket": ("export_not_supported", "This asset is already in the user's own bucket; there is "
                                                       "nothing to export. Don't retry."),
    "nothing to export": ("export_not_supported", "This asset is linked from its source (Hugging Face, CivitAI or a "
                                                  "URL), so Meshive stores nothing to export. Don't retry."),
    "not uploadable": ("export_not_supported", "Only assets kept in Meshive storage can be uploaded. Don't retry."),
    "asset not available": ("asset_not_ready", "The asset isn't ready (still uploading, being moved or deleted). "
                                               "Check its status with the assets tool and try again once it is active."),
    "token required": ("hf_token_required", "Nothing was started. `hf_token_id` must be a saved Hugging Face token "
                                            "with write access (an id from source_credentials); tokens are added in "
                                            "the console."),
    "storage credential not found": ("not_found", "Use a bucket_id from buckets."),
    "storage credential error": ("bucket_access_failed",
                                 "The saved keys can't read this bucket; the user must fix or re-add the connection "
                                 "in the console (Settings → Storage). Don't retry."),
    # --- upload_asset with `asset` (adding files to a collection) ---
    "file path already exists": ("file_exists",
                                 "These paths are already in the collection and adding never replaces. Delete them "
                                 "first with delete_asset_files if the user wants them replaced, or rename the new "
                                 "files."),
    "not a collection": ("not_a_collection", "Only collections take more files; upload a new asset instead."),
    "linked asset": ("asset_not_managed", "Only collections in Meshive storage take more files."),
    "unsupported asset": ("asset_not_managed", "Only collections in Meshive storage take more files."),
    "unsupported storage": ("asset_not_managed", "Only collections in Meshive storage take more files."),
    "asset not active": ("asset_not_ready", "Files can be added only to an active collection. Check its status with "
                                            "the assets tool."),
    # Adding files and exporting both answer this: the asset's files aren't complete in Meshive storage.
    "asset not ready": ("asset_not_ready", "Nothing was changed. The asset's files aren't ready in Meshive storage "
                                           "(still uploading, or an older asset whose upload never finished). Tell "
                                           "the user; don't retry."),
    "move in progress": ("asset_moving", "The collection is being moved to other storage. Wait until the move "
                                         "ends (the assets tool shows its status), then start the upload again."),
    "upload conflict": ("upload_in_progress", "Another upload into this collection is still open. Finish or cancel "
                                              "it first (finish_upload), or wait until it expires."),
    "upload batch conflict": ("upload_in_progress", "Another upload into this collection is still open. Finish or "
                                                    "cancel it first (finish_upload), or wait until it expires."),
    "upload already completed": ("upload_already_completed",
                                 "The files were added; delete them with delete_asset_files if unwanted."),
}

# Accounts without a completed payment can take only so much out of Meshive storage per day (download links, exports).
NEXT_STEP_EXPORT_LIMIT = ("Accounts without a completed payment can export a limited amount per day, reset at 00:00 "
                          "UTC. Tell the user; don't retry today.")
_EXPORT_LIMIT_TITLE = "daily export limit reached"


def asset_write_error(exc: BaseException | None) -> ToolError | None:
    """The asset tools' own reading of a server refusal, or None to keep the general one."""
    if not isinstance(exc, MeshiveAPIError) or exc.status_code not in (400, 403, 404, 409, 422):
        return None
    found = _ASSET_WRITE_CODES.get((exc.title or "").strip().lower())
    if found is None:
        return None
    code, next_step = found
    return tool_error(code, exc.message or exc.title, next_step, **_detail_extras(exc))


# --- Assets in a pod (add_pod_asset, remove_pod_asset) ---
# Told apart by detail.title, for these two tools only: "Pod Not Running" or "Asset Not Available" mean something else
# on other routes.
NEXT_STEP_POD_CONFIGURATION = ("It is part of the pod's configuration: change or remove it with update_pod "
                               "(add_input_assets / remove_input_assets), which restarts the pod. Ask the user first.")
NEXT_STEP_NO_INPUT_SLOT = ("Pick a role from the pod's `input_slots` (pods with include_assets). To place the asset "
                           "elsewhere, ask the user whether to use update_pod add_input_assets, which restarts the pod.")
NEXT_STEP_INVALID_TARGET_DIR = "Use the slot path or one folder directly inside it."
NEXT_STEP_PLACEMENT_BUSY = ("Wait until the row settles (pods with include_assets shows it active or failed), then "
                            "preview again.")
NEXT_STEP_INJECTION_IN_PROGRESS = ("This asset is still being added to or removed from this pod for that role. Wait "
                                   "until pods with include_assets shows the row active or failed (or gone), then "
                                   "preview again; don't remove it meanwhile.")
_PLACEMENT_CONFLICT = ("The placement collides with something in the pod (the message says what). Pick another "
                       "target_dir or other files, then preview again.")
_POD_ASSET_CODES = {
    "pod not running": ("pod_not_running",
                        "The pod isn't running, so nothing was changed. If it is stopped, ask the user whether to "
                        "start it (start_pod) and call again, or to use update_pod add_input_assets / "
                        "remove_input_assets, which changes the pod's configuration with a restart. If it is deleted "
                        "(pods shows it terminated), its files are gone with it: don't retry."),
    "pod busy": ("pod_busy", "Another operation on this pod (starting, stopping or an edit) is still running. Wait "
                             "until the pods tool shows it running or stopped."),
    "pod cannot receive assets": ("pod_cannot_receive_assets",
                                  "This pod can't take assets without a restart. Ask the user whether to use update_pod "
                                  "add_input_assets, which restarts it."),
    "pod cannot remove assets": ("pod_cannot_remove_assets",
                                 "This pod can't remove assets while it runs. Ask the user whether to use update_pod "
                                 "remove_input_assets, which restarts it."),
    "invalid role": ("no_input_slot", NEXT_STEP_NO_INPUT_SLOT),
    "input path missing": ("no_input_slot", NEXT_STEP_NO_INPUT_SLOT),
    "path outside slot": ("invalid_target_dir", NEXT_STEP_INVALID_TARGET_DIR),
    "reserved path": ("invalid_target_dir", NEXT_STEP_INVALID_TARGET_DIR),
    "folder too deep": ("invalid_target_dir", NEXT_STEP_INVALID_TARGET_DIR),
    "asset frozen": ("asset_frozen", NEXT_STEP_ASSET_FROZEN),
    "asset not available": ("asset_unavailable", "The asset is deleted or being deleted. Check it with the assets "
                                                 "tool."),
    "asset not found": ("asset_unavailable", "The asset is deleted or being deleted. Check it with the assets tool."),
    "asset not ready": ("asset_not_ready", "The asset is still being uploaded or prepared. Wait until the assets tool "
                                           "shows it active, then preview again."),
    "reference asset not supported": ("asset_not_placeable",
                                      "This asset only points at a folder in the user's bucket and can't be placed in "
                                      "a pod yet. Don't retry."),
    "source token missing": ("source_token_missing",
                             "The token this asset was imported with was removed. The user must add it again in the "
                             "console."),
    "injection in progress": ("injection_in_progress", NEXT_STEP_INJECTION_IN_PROGRESS),
    "already connected": ("already_connected",
                          "This asset is already placed in this pod for that role with the same files; nothing to do."),
    "already placed elsewhere": ("placed_elsewhere",
                                 "Nothing was changed. The asset is already placed for this role in another folder "
                                 "(the message names it). To change its files, call again without target_dir (or with "
                                 "that folder). To move it, ask the user whether to remove it first with "
                                 "remove_pod_asset (for an `origin` deploy row, update_pod remove_input_assets, which "
                                 "restarts the pod)."),
    "part of pod configuration": ("part_of_pod_configuration", NEXT_STEP_POD_CONFIGURATION),
    "slot already filled": ("slot_filled", "The slot takes one asset. Ask the user whether to remove the current one "
                                           "(remove_pod_asset) first."),
    "overlaps a watched folder": ("placement_conflict", _PLACEMENT_CONFLICT),
    "input assets collide": ("placement_conflict", _PLACEMENT_CONFLICT),
    "selection cannot be verified": ("placement_conflict", _PLACEMENT_CONFLICT),
    "asset configuration conflict": ("asset_config_conflict", "Something changed the pod's assets meanwhile; preview "
                                                              "again."),
    "move in progress": ("asset_moving", "The asset is being moved to other storage. Wait until the move ends (the "
                                         "assets tool shows its status), then preview again."),
    "asset usage blocked": ("asset_usage_blocked", "Credit is exhausted; the user must top up in the console."),
    "placement not found": ("not_found", "List pods with include_assets for current connection_id values."),
    "placement busy": ("placement_busy", NEXT_STEP_PLACEMENT_BUSY),
}


def pod_asset_error(exc: BaseException | None) -> ToolError | None:
    """add_pod_asset's and remove_pod_asset's own reading of a server refusal, or None to keep the general one."""
    if not isinstance(exc, MeshiveAPIError) or exc.status_code not in (400, 403, 404, 409, 422):
        return None
    found = _POD_ASSET_CODES.get((exc.title or "").strip().lower())
    if found is None:
        return None
    code, next_step = found
    return tool_error(code, exc.message or exc.title, next_step, **_detail_extras(exc))


# --- Image inspection and custom templates (templates with `image`, save_template) ---
# Told apart by detail.title, for these tools only: "Access Denied" or "Authentication Failed" mean something else on
# other routes. A registry login that can't be used comes as a 404 titled "Not Found", told apart by its message.
NEXT_STEP_IMAGE_NOT_FOUND = "Nothing was saved. Check the name, tag and platform with the user; don't guess another image."
NEXT_STEP_REGISTRY_LOGIN = ("Nothing was saved. Pass registry_login_id from source_credentials; logins are added or "
                            "fixed in the console. Never ask the user for the password.")
NEXT_STEP_REGISTRY_UNUSABLE = "Nothing was saved. Ask the user to re-add the login in the console."
_TEMPLATE_CODES = {
    "image not found": ("image_not_found", NEXT_STEP_IMAGE_NOT_FOUND),
    "invalid image reference": ("image_not_found", NEXT_STEP_IMAGE_NOT_FOUND),
    "loading image failed": ("image_not_found", NEXT_STEP_IMAGE_NOT_FOUND),
    "registry credential required": ("registry_credential_required", NEXT_STEP_REGISTRY_LOGIN),
    "authentication failed": ("registry_credential_required", NEXT_STEP_REGISTRY_LOGIN),
    "access denied": ("registry_credential_required", NEXT_STEP_REGISTRY_LOGIN),
    "registry mismatch": ("registry_mismatch", "Nothing was saved. Pick the login whose `server` is the image's host "
                                               "(source_credentials `registry_logins`)."),
    "registry credential incomplete": ("registry_credential_unusable", NEXT_STEP_REGISTRY_UNUSABLE),
    "too many endpoints": ("too_many_ports", "Nothing was saved. The message says how many ports a template can have; "
                                             "drop some."),
    "command too long": ("invalid_argument", "Nothing was saved. Shorten `command` and call again."),
}
_REGISTRY_LOGIN_MISSING = ("registry auth not found", "registry auth secret not found")


def template_error(exc: BaseException | None) -> ToolError | None:
    """The image and template tools' own reading of a server refusal, or None to keep the general one."""
    if not isinstance(exc, MeshiveAPIError) or exc.status_code not in (400, 403, 404, 409, 422):
        return None
    title = (exc.title or "").strip().lower()
    if title == "registry rate limited":
        # The registry's own limit, sent as a 400: waiting helps, as with Meshive's.
        return tool_error("rate_limited", exc.message or exc.title,
                          "Nothing was saved. The image's registry is limiting requests. Do not retry immediately. "
                          "Wait at least 60s before calling this tool again.", retry_after=60)
    if (exc.message or "").strip().lower() in _REGISTRY_LOGIN_MISSING:
        return tool_error("registry_credential_unusable", exc.message, NEXT_STEP_REGISTRY_UNUSABLE)
    found = _TEMPLATE_CODES.get(title)
    if found is None:
        if title == "idempotency-key reused":
            # Not the template's settings: the general reading (operation_id_reused) applies.
            return None
        if exc.status_code == 422:
            # The request model's own check (an env name the platform reserves, a field too long, ...).
            return invalid_argument(exc.message or "The server rejected the template's settings.")
        return None
    code, next_step = found
    return tool_error(code, exc.message or exc.title, next_step, **_detail_extras(exc))


# --- Serving writes (deploy_serving, scale_serving, register_model) ---
# Told apart by detail.title, for these tools only. "Serving Not Found" from scale_serving comes after the serving was
# read, so it means that another member deployed it, not a wrong id.
NEXT_STEP_MODALITY_REQUIRED = ("Hugging Face doesn't say what this model does. Call detect_model and ask the user; then "
                               "call again with takes_image set.")
_SERVING_WRITE_CODES = {
    "sharing not available": ("sharing_not_available",
                              "Nothing was changed. Idle-capacity sharing is only for official text models served "
                              "with vLLM; deploy or scale without share_idle_capacity."),
    "storage credential not found": ("output_bucket_not_found",
                                     "Use a bucket_id from buckets in this serving's workspace."),
    "storage credential not verified": ("output_bucket_not_verified",
                                        "The bucket must be verified in the console first."),
    "serving not changeable": ("serving_not_changeable",
                               "Nothing was changed. The serving is being deleted, is already deleted, or is in error "
                               "(the message says which), so it can't be changed in this state; waiting won't help. "
                               "Don't call again. If the user wants, deploy a new serving with deploy_serving (a "
                               "serving in error is deleted with delete_serving first)."),
    "serving not found": ("not_serving_owner", "Only the member who deployed it can change it."),
    "modality required": ("modality_required", NEXT_STEP_MODALITY_REQUIRED),
}


def serving_write_error(exc: BaseException | None) -> ToolError | None:
    """The serving write tools' own reading of a server refusal, or None to keep the general one."""
    if not isinstance(exc, MeshiveAPIError) or exc.status_code not in (400, 403, 404, 409, 422):
        return None
    found = _SERVING_WRITE_CODES.get((exc.title or "").strip().lower())
    if found is None:
        return None
    code, next_step = found
    return tool_error(code, exc.message or exc.title, next_step, **_detail_extras(exc))


def _server_code(exc: MeshiveAPIError) -> str | None:
    detail = exc.raw.get("detail") if isinstance(exc.raw, dict) else None
    return detail.get("code") if isinstance(detail, dict) else None


def _lock_route_refusal(exc: NotFoundError) -> ToolError | None:
    """404s of the port lock and access key calls that aren't a wrong pod or port."""
    message = exc.message or ""
    if _server_code(exc) == _LOCK_DISABLED_CODE:
        return lock_unavailable("disabled", pod_request=False)
    if exc.title is None and "does not support port locks" in message:
        # What the SDK makes of an API server without these routes.
        return lock_unavailable("unsupported", pod_request=False)
    if message.strip().lower().startswith("access key not found"):
        return tool_error("access_key_not_found", message,
                          "No key with this id on this port: it was revoked, it expired more than 30 days ago, or it was "
                          "rolled (a roll gives the key a new id). List the port's keys with access_keys and use an `id` "
                          "from there. Do not guess.")
    return None


def _detail_extras(exc: MeshiveAPIError) -> dict[str, Any]:
    """Everything in the server's detail object except title/message, passed on to the model as-is.
    A server `code` (e.g. same_node_spec_mismatch) is kept as `server_code` so it doesn't clash with our own `code`."""
    detail = exc.raw.get("detail") if isinstance(exc.raw, dict) else None
    if not isinstance(detail, dict):
        return {}
    reserved = {"code", "next_step"}
    return {(f"server_{k}" if k in reserved else str(k)): v for k, v in detail.items() if k not in ("title", "message")}


def _conflict(exc: ConflictError) -> ToolError:
    if _server_code(exc) == _LOCK_DISABLED_CODE:
        # The server's own refusal of a pod request that locks a port, should the SDK pass it on as it came.
        return lock_unavailable("disabled", pod_request=True)
    title = (exc.title or "").strip().lower()
    code, next_step = _CONFLICT_CODES.get(title, ("conflict", "Check the current state with the matching list tool "
                                                              "before retrying."))
    extras = _detail_extras(exc)
    if code == "data_loss_consent_required" and "placement" not in extras:
        # Only start_pod names a placement; without one the refusal comes from update_pod's allow_node_migration.
        next_step = _DATA_LOSS_EDIT_STEP
    return tool_error(code, exc.message or exc.title or "Conflict.", next_step, **extras)


def _bad_request(exc: MeshiveAPIError) -> ToolError:
    title = (exc.title or "").strip().lower()
    code, next_step = _BAD_REQUEST_CODES.get(title, ("invalid_request", "The server rejected the request. Fix the "
                                                                        "arguments as described and retry once."))
    return tool_error(code, exc.message or exc.title or f"HTTP {exc.status_code}", next_step, **_detail_extras(exc))


def translate(exc: BaseException) -> ToolError:
    """SDK/network exceptions → ToolError. A ToolError passes through unchanged."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ConfigurationError):
        return tool_error("no_api_key", "No Meshive API key was provided.", NEXT_STEP_NO_KEY)
    if isinstance(exc, AuthenticationError):
        return tool_error("invalid_api_key", exc.message or "Invalid API key.", NEXT_STEP_INVALID_KEY)
    if isinstance(exc, PermissionDeniedError):
        msg = exc.message or "Forbidden."
        low = msg.lower()
        if "scope" in low:
            return tool_error("write_scope_required", msg, NEXT_STEP_WRITE_SCOPE)
        # "You are not member of this workspace" is the SDK routes' admin gate for a workspace the caller isn't in.
        if ("member of workspace" in low or "not a member" in low or "not member of" in low
                or (exc.title or "").strip().lower() == "not a member"):
            # The backend answers a nonexistent or someone else's workspace id with 403 — for the model that's a "wrong id".
            return tool_error("unknown_workspace", msg,
                              "The workspace id is wrong or not the user's. Call the workspaces tool and use "
                              "the `namespace_name` value (not the label).")
        return tool_error("forbidden", msg, NEXT_STEP_FORBIDDEN)
    if isinstance(exc, NotFoundError):
        if exc.title == "Pod Creation Failed":
            # The pod is gone because creating it failed, not because of a wrong id — re-listing won't find it.
            return tool_error("pod_creation_failed", exc.message or "Pod creation failed.",
                              "Tell the user the reason. Don't retry this pod; fix the cause (template, command, "
                              "inputs) and create a new pod.")
        refusal = _lock_route_refusal(exc)
        if refusal is not None:
            return refusal
        special = _NOT_FOUND_CODES.get((exc.title or "").strip().lower())
        if special is not None:
            return tool_error(special[0], exc.message or exc.title, special[1])
        return tool_error("not_found", exc.message or "Not found.", NEXT_STEP_NOT_FOUND)
    if isinstance(exc, RateLimitError) and (exc.title or "").strip().lower() == _EXPORT_LIMIT_TITLE:
        # A daily allowance, not a burst limit: waiting seconds doesn't help.
        return tool_error("export_limit_reached", exc.message or "Daily export limit reached.", NEXT_STEP_EXPORT_LIMIT)
    if isinstance(exc, RateLimitError):
        wait = int(exc.retry_after) if exc.retry_after else 30
        return tool_error("rate_limited", exc.message or "Rate limit exceeded.",
                          f"Do not retry immediately. Wait at least {wait}s before calling any Meshive tool again.",
                          retry_after=wait)
    if isinstance(exc, InsufficientCreditError):
        return tool_error("insufficient_credit", exc.message or "Insufficient credit.",
                          f"Ask the user to top up credit at {CONSOLE_URL} before retrying. Do not retry on your own.")
    if isinstance(exc, EndpointLockUnavailableError):
        # The SDK's own message names its Python arguments; the agent gets this server's argument names instead.
        return lock_unavailable(exc.reason, pod_request=True)
    if isinstance(exc, ConflictError):
        return _conflict(exc)
    if isinstance(exc, MeshiveAPIError):
        status = exc.status_code
        msg = exc.message or f"HTTP {status}"
        if status == 402:
            return tool_error("insufficient_credit", msg,
                              f"Ask the user to top up credit at {CONSOLE_URL} before retrying.")
        if status == 409:
            return tool_error("conflict", msg,
                              "Check the current state with the matching list tool before retrying.")
        if status in (400, 422):
            return _bad_request(exc)
        if status == 503 and (exc.title or "").strip().lower() == "fan status unavailable":
            # The machine's agent didn't answer a live fan reading; Meshive itself is up.
            return tool_error("fan_status_unavailable", msg, "Try again in a minute; the node agent didn't answer.")
        if status == 503:
            return tool_error("temporarily_unavailable", msg, NEXT_STEP_UNAVAILABLE.format(retry_after=30),
                              retry_after=30)
        if status >= 500:
            return tool_error("server_error", msg, NEXT_STEP_UNAVAILABLE.format(retry_after=15),
                              retry_after=15)
        return tool_error("api_error", msg, "Tell the user what the server said. Do not retry blindly.",
                          status=status)
    # RemoteProtocolError: the API closed the connection without answering, as when one of its workers restarts while
    # the request is in flight. Like a read timeout, a write may or may not have been applied.
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return tool_error("temporarily_unavailable", f"Could not reach the Meshive API: {exc.__class__.__name__}",
                          NEXT_STEP_UNAVAILABLE.format(retry_after=15), retry_after=15)
    if StorageConnectionError is not None and isinstance(exc, StorageConnectionError):
        return tool_error("storage_connection_failed", str(exc) or "The storage could not be opened for SSH.",
                          "The storage's access pod could not be prepared, so there is no login. Tell the user; they "
                          "can try connecting again in a moment or check the storage in the console. Don't retry on "
                          "your own.")
    if isinstance(exc, WaitTimeoutError):
        # The SDK stopped waiting; the server keeps going with what it already accepted.
        return tool_error("wait_timeout", str(exc) or "Timed out waiting.",
                          "It is still in progress on the server. Check its state with the matching tool before doing "
                          "anything else, and don't start it again.")
    if isinstance(exc, MeshiveError):
        return tool_error("sdk_error", str(exc), "Tell the user. Do not retry.")
    if type(exc) is ValueError:
        # The SDK checks argument shapes (local_volumes items, mount paths, ...) before sending and raises a plain
        # ValueError that says what is wrong; nothing was sent.
        return invalid_argument(str(exc) or "Invalid argument.")
    # Anything else is treated as a server bug — the SDK wraps it in UnexpectedToolError and logs the traceback.
    raise exc
