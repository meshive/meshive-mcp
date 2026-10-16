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

KEYS_PAGE = f"{CONSOLE_URL}, workspace Settings > API keys"
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
NEXT_STEP_UNAVAILABLE = ("Wait {retry_after}s. For a read, retry once. For a write, check operation_status first; "
                        "reuse the original operation_id and arguments if retrying. Never create a new operation for an unknown outcome.")


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
    # unpreserved workspace files. Nothing was started; detail.placement names the move to consent to.
    "data loss consent required": ("data_loss_consent_required",
                                   "Nothing was started. Starting this pod with the `placement` in this error moves it "
                                   "to another machine and permanently deletes its unpreserved workspace files. Show "
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
                                 "(update_pod or update_storage `name`), ask the user for another name."),
    "price exceeds cap": ("price_exceeds_cap", "The estimate is above the user's price cap. Show `pricePerHourUsd` and ask "
                                               "whether to raise the cap or choose cheaper hardware."),
    "storage in use": ("storage_in_use", "The storage is mounted by the pods in `linkedPods`. Stop/delete or detach them "
                                         "first (ask the user)."),
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
                                "move to another machine, which permanently deletes its unpreserved workspace files. "
                                "Tell the user why and ask whether to move it. Only if they agree, call update_pod again "
                                "with allow_node_migration=true, confirm=true and the same operation_id; its preview "
                                "says whether the move also needs separate consent to the file loss "
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
}

# update_pod's own data-loss refusal carries no placement (start_pod's does): the move would come from
# allow_node_migration.
_DATA_LOSS_EDIT_STEP = ("Nothing was changed. If this edit moves the pod to another machine (allow_node_migration), its "
                        "unpreserved workspace files are permanently deleted. Show the user that warning for this pod and "
                        "get separate explicit consent to the loss. Only then call update_pod again with the same "
                        "arguments plus allow_data_loss=true, confirm=true and the same operation_id. Without that "
                        "consent, call it without allow_node_migration instead.")

# The kind of 400 is told apart by detail.title too. Extra info (allowed, keys, port, name, maxLength, templatePorts,
# attached, paths, currentSizeGb, failed, available) is passed on.
_BAD_REQUEST_CODES = {
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
    "storage required": ("storage_required", "This template needs a persistent volume. Attach network storage in "
                                             "`volumes` or create local storage in `local_volumes`; ask the user which."),
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
}


def _detail_extras(exc: MeshiveAPIError) -> dict[str, Any]:
    """Everything in the server's detail object except title/message, passed on to the model as-is.
    A server `code` (e.g. same_node_spec_mismatch) is kept as `server_code` so it doesn't clash with our own `code`."""
    detail = exc.raw.get("detail") if isinstance(exc.raw, dict) else None
    if not isinstance(detail, dict):
        return {}
    reserved = {"code", "next_step"}
    return {(f"server_{k}" if k in reserved else str(k)): v for k, v in detail.items() if k not in ("title", "message")}


def _conflict(exc: ConflictError) -> ToolError:
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
        if "member of workspace" in low or "not a member" in low:
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
        return tool_error("not_found", exc.message or "Not found.", NEXT_STEP_NOT_FOUND)
    if isinstance(exc, RateLimitError):
        wait = int(exc.retry_after) if exc.retry_after else 30
        return tool_error("rate_limited", exc.message or "Rate limit exceeded.",
                          f"Do not retry immediately. Wait at least {wait}s before calling any Meshive tool again.",
                          retry_after=wait)
    if isinstance(exc, InsufficientCreditError):
        return tool_error("insufficient_credit", exc.message or "Insufficient credit.",
                          f"Ask the user to top up credit at {CONSOLE_URL} before retrying. Do not retry on your own.")
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
        if status == 503:
            return tool_error("temporarily_unavailable", msg, NEXT_STEP_UNAVAILABLE.format(retry_after=30),
                              retry_after=30)
        if status >= 500:
            return tool_error("server_error", msg, NEXT_STEP_UNAVAILABLE.format(retry_after=15),
                              retry_after=15)
        return tool_error("api_error", msg, "Tell the user what the server said. Do not retry blindly.",
                          status=status)
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError)):
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
