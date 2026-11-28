"""Shared by the tool modules: registration decorator, annotations, exception translation wrapper."""
from __future__ import annotations

import functools
import inspect
import json
import re
import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .. import money, oauth, oauth_confirm
from ..auth import api_key
from ..errors import UNAVAILABLE_WRITE_STEP, translate, tool_error

T = TypeVar("T")
MAX_RESPONSE_BYTES = 1024 * 1024
# Errors whose next_step already says whether and how to call again, so the generic "retry with the same operation_id and
# the same arguments" advice would contradict it: a failed create is final, a start refused for data-loss consent is
# called again with the placement it names and allow_data_loss=true once the user agrees, an edit refused for a
# move, local storage or watched folders is called again with the argument the user agreed to, a taken name is
# checked against the list or changed, and a refused workspace delete is checked again without confirm or left alone.
# Host refusals are fixed and asked about again (new times, a recipient from the machine's clients, the reason the
# machine can't be listed, the window's state), waited out (a port check), or not retried (a removal in progress, a
# login the machine doesn't have). A request for a locked port that the server can't lock goes back to the user, who
# may change it; a failed lock change is repaired port by port, not by sending the edit again; and an access key call
# without a clear answer, or one whose answer lost the value, is followed by the key list and the user's decision,
# because calling again issues another key value. An asset change or deletion refused because the asset is in use,
# frozen, still being uploaded into, or can't be changed says what has to happen first; a file selection that is the
# whole asset, too large, empty or changed since the preview is previewed again, not sent again. A serving replica
# whose log can't be read names the replicas to read instead, or says to wait. An invalid argument is refused before
# anything runs (locally, or by a server 400 that releases the operation_id), so the fix is a different argument, not
# the same arguments again. An operation_id already bound to other arguments is looked up with operation_status
# first, not sent again.
_OWN_RETRY_ADVICE_CODES = {"invalid_argument", "asset_in_use", "asset_frozen", "upload_in_progress", "asset_not_editable",
                           "asset_type_locked", "whole_asset_delete_required", "selection_changed", "too_many_files",
                           "no_files_matched", "replica_not_found", "replica_logs_expired", "replica_logs_unavailable",
                           "endpoint_lock_unavailable", "port_lock_change_failed", "access_key_outcome_unknown",
                           "access_key_value_missing", "operation_id_reused",
                           "name_taken", "pod_creation_failed", "data_loss_consent_required", "node_migration_required",
                           "cannot_change_resources", "harvest_roots_orphaned", "model_set_unavailable",
                           "hf_token_required", "hf_license_not_accepted", "hf_access_not_approved",
                           "hf_token_rejected", "workspace_not_empty", "workspace_not_deletable",
                           "insufficient_notice", "maintenance_overlap", "maintenance_state", "recipient_not_allowed",
                           "message_rejected", "listing_refused", "port_check_cooldown", "delete_in_progress",
                           "credentials_not_available", "feedback_unavailable", "feedback_rate_limited"}
# Asset files, buckets and exports: a bucket the saved keys can't read, the daily export allowance, an asset that can't
# be exported or isn't ready, and the refusals of adding files to a collection (a path that exists, not a collection,
# not in Meshive storage, being moved, already completed, too large for this server) each say what has to change.
_OWN_RETRY_ADVICE_CODES |= {"bucket_access_failed", "export_limit_reached", "export_not_supported", "asset_not_ready",
                            "file_exists", "not_a_collection", "asset_not_managed", "asset_moving",
                            "upload_already_completed", "file_too_large_for_mcp"}
# Assets in a pod: a pod that isn't running or can't take or remove assets without a restart, a slot or folder that
# doesn't fit, an asset that can't be placed, a request already in progress or done, a row of the pod's configuration,
# a filled slot, a busy row, no credit, a placement that collides, a deleted asset, the pod's assets changed meanwhile,
# and an update_pod connection_id that isn't an active input asset each say what has to change first.
_OWN_RETRY_ADVICE_CODES |= {"pod_not_running", "pod_cannot_receive_assets", "pod_cannot_remove_assets",
                            "no_input_slot", "role_required", "invalid_target_dir", "asset_not_placeable",
                            "source_token_missing", "injection_in_progress", "already_connected", "placed_elsewhere",
                            "part_of_pod_configuration", "slot_filled", "placement_busy", "asset_usage_blocked",
                            "input_asset_not_found", "placement_conflict", "asset_unavailable",
                            "asset_config_conflict"}
# An asset list filter the API server doesn't apply yet: listing again with it gives the same unfiltered answer.
_OWN_RETRY_ADVICE_CODES |= {"filter_not_supported"}
# Messages to hosts, images and custom templates: a host the workspace can't reach, an image that can't be read or a
# registry login that doesn't fit or can't be used, too many ports, and the local refusals of save_template (an image
# change, a secret's value) each say what has to change first.
_OWN_RETRY_ADVICE_CODES |= {"host_not_reachable", "image_not_found", "registry_credential_required",
                            "registry_mismatch", "registry_credential_unusable", "too_many_ports",
                            "image_change_not_supported", "secret_value_not_accepted"}
# Servings and tasks: sharing that isn't offered or not yet agreed to, an output bucket that isn't there or verified, a
# serving that can't change now or was deployed by another member, a model whose input Hugging Face doesn't state or
# that isn't found, a hidden secret value that can't be resubmitted, and an output upload with nothing left to cancel
# each say what has to change first.
_OWN_RETRY_ADVICE_CODES |= {"sharing_not_available", "sharing_consent_required", "output_bucket_not_found",
                            "output_bucket_not_verified", "serving_not_changeable", "not_serving_owner",
                            "modality_required", "model_not_found", "masked_secret_value", "nothing_to_cancel"}
# Inference requests, webhooks and inference keys: a request that isn't queued any more, a Slack or Discord URL (a
# secret) or a URL the server refuses, a turned-off endpoint, a request, endpoint, delivery or key id that isn't there,
# a key only its creator can roll, an expired key (a new one is made instead), and an inference key call without a
# clear answer, or whose answer lost the value (calling again issues another value), each say what to do instead of
# calling again.
_OWN_RETRY_ADVICE_CODES |= {"request_not_cancellable", "request_not_found", "secret_url_not_accepted",
                            "invalid_webhook_url", "webhook_disabled", "webhook_not_found", "delivery_not_found",
                            "inference_key_outcome_unknown", "inference_key_value_missing", "inference_key_not_found",
                            "inference_key_not_rollable", "inference_key_expired"}
# Assets from a bucket and saved credentials: keys or a folder that aren't in the bucket, a copy out of a bucket folder
# that can't be done as asked, a credential id that isn't there, and a bucket that isn't verified each say what has to
# change first.
_OWN_RETRY_ADVICE_CODES |= {"bucket_objects_not_found", "bucket_prefix_empty", "subset_not_fetchable",
                            "credential_not_found", "bucket_not_verified"}
# Members and invitations: a member who can't be removed (yourself, the only admin, the billing owner, not a member)
# and an invitation that isn't there each say what to do instead of calling again.
_OWN_RETRY_ADVICE_CODES |= {"member_not_removable", "invite_not_found"}
# Fan curves: a curve the server refuses is fixed and previewed again, and a machine without GPUs has no fan curve.
_OWN_RETRY_ADVICE_CODES |= {"invalid_fan_curve", "not_gpu_machine"}
# A webhook URL that is only the masked form of the stored one, and an API server without fan curve routes, each say
# what to do instead of calling again.
_OWN_RETRY_ADVICE_CODES |= {"masked_url_given", "fan_curve_not_supported"}
_operation: ContextVar[dict | None] = ContextVar("mcp_operation", default=None)
# SDK methods that take an Idempotency-Key: the server keeps an operation record for them, so a retry with the same
# operation_id gets the first answer back and operation_status can look the operation up.
_WRITES = {"create_pod", "stop_pod", "start_pod", "restart_pod", "delete_pod", "update_pod", "create_storage",
           "update_storage", "delete_storage", "deploy_serving", "scale_serving", "pause_serving", "delete_serving",
           "submit_task", "stop_task", "register_model", "delete_model", "import_asset", "set_watched_folders",
           "create_workspace", "update_workspace", "delete_workspace",
           "schedule_maintenance", "update_maintenance", "cancel_maintenance", "end_maintenance",
           "extend_maintenance", "set_machine_listing", "retry_machine_setup", "delete_machine",
           "send_host_message", "mark_host_messages_read", "set_port_lock", "set_port_preflight",
           "revoke_access_key", "revoke_browser_sessions", "revoke_workspace_browser_sessions", "send_feedback",
           "update_asset", "delete_asset", "delete_asset_files"}
# Copying an asset out of Meshive storage, and making catalog files assets.
_WRITES |= {"export_asset", "upload_asset_to_hf", "import_open_assets"}
# Adding an asset to a pod without a restart, and taking one out.
_WRITES |= {"add_pod_asset", "remove_pod_asset"}
# Writing from a workspace to a host and marking the host's replies read, and saving or deleting a custom template.
_WRITES |= {"message_host", "mark_workspace_messages_read", "create_template", "update_template", "delete_template"}
# Deleting finished tasks, and stopping a task's output upload.
_WRITES |= {"delete_tasks", "discard_task_outputs"}
# Canceling a queued inference request, saving, testing and deleting a webhook endpoint, and revoking an inference key.
_WRITES |= {"cancel_inference_request", "create_webhook", "update_webhook", "test_webhook", "delete_webhook",
            "revoke_inference_key"}
# Making assets from a connected bucket, copying files out of a bucket folder, deleting a saved credential or bucket
# connection, and choosing where task outputs go by default.
_WRITES |= {"register_bucket_objects", "register_bucket_prefix", "fetch_reference_subset", "delete_hf_token",
            "delete_civitai_key", "delete_registry_login", "delete_storage_credential", "set_asset_storage_setting"}
# Removing a member, canceling an invitation, and changing the account's display name.
_WRITES |= {"remove_member", "cancel_invite", "update_profile"}
# Setting a hosted GPU machine's fan curve.
_WRITES |= {"set_fan_curve"}
# SDK methods that change something but are sent without an operation_id, so the server keeps no operation record and
# a repeated call runs again. connect_storage, ssh_access, reveal_machine_credentials and issue_install_command are
# left out of _WRITES on purpose: their responses carry a password or an install command, which the server must not
# keep as a replayable record. retry_port_check has no operation record either; its cooldown keeps it from repeating.
# create_access_key and roll_access_key answer with a key's value, which the server must not keep either: every call
# issues another value, and the SDK doesn't send one again after a timeout.
_UNRECORDED = {"connect_storage", "ssh_access", "reveal_machine_credentials", "issue_install_command",
               "retry_port_check", "create_access_key", "roll_access_key",
               # A command run in a pod and the steps of a file upload: the server keeps no operation record of them
               # (it would hold the command's output or the upload links), so calling again runs or starts it again.
               "run_command", "start_asset_upload", "complete_asset_upload", "abort_asset_upload"}
# The steps of adding files to a collection: their answers hold the upload's token or links.
_UNRECORDED |= {"start_asset_file_upload", "asset_file_upload_part_urls", "complete_asset_file_upload",
                "abort_asset_file_upload"}
# An inference key's value, like a port access key's: every call issues another value.
_UNRECORDED |= {"create_inference_key", "roll_inference_key"}


def _bounded_result(result: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    response = CallToolResult(content=[TextContent(type="text", text=text)],
                              structured_content=result, is_error=is_error)
    # Bound the final envelope, including the duplicated structured/text payload.
    if len(response.model_dump_json(by_alias=True).encode("utf-8")) > MAX_RESPONSE_BYTES:
        body = {"code": "response_too_large", "message": "The response exceeds 1 MiB.",
                "next_step": "Request fewer items or log lines. Check the resource state before retrying a write."}
        if result.get("operation_id"):
            body["operation_id"] = result["operation_id"]
        if result.get("operation_lookup"):
            body["operation_lookup"] = result["operation_lookup"]
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(body))], is_error=True)
    return response

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
# Create/deploy/submit: repeating the same call adds resources (the server's Idempotency-Key is for SDK retries).
CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
# State changes that converge on repeated calls.
MUTATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)
# Reveal a secret or issue something anew: every call reveals a login again or issues a new one-time password or
# install command, so repeating it does more than the first call (machine logins and install commands also email the
# account owner every time).
REVEAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
# Repeating a command can change the pod again, and rolling a key replaces its value.
DESTRUCTIVE_NON_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

CONFIRM_STEP = ("Show this to the user and ask for an explicit go-ahead. Only then call this tool again with the same "
                "arguments and confirm=true. Never set confirm=true on your own.")


def preview(kind: str, payload: dict[str, Any], question: str) -> dict[str, Any]:
    """confirm=false response: states that nothing was created or deleted and tells the model what to do next."""
    return {"confirmed": False, "action": kind, **payload, "question": question, "next_step": CONFIRM_STEP}


def bind_pod(out: dict[str, Any], pod_name: str, *, retries: bool = True) -> dict[str, Any]:
    """A confirmation goes by id: a display name can move to another pod between the preview and the confirmation.
    retries=False for a call that is never sent again after an unclear outcome (an access key's value is issued once
    per call), whose preview mustn't mention retrying."""
    out["pod_name"] = pod_name
    use = "for confirmation and every retry" if retries else "for the confirmation"
    out["next_step"] += (f" Set `pod` to the returned `pod_name` ({pod_name}) {use}; display names can change or be "
                         "reused.")
    return out


# What the console shows for a serving replica that is still being set up and has no pod (the server's setup steps,
# in order). Any other stage value passes through unchanged.
SETUP_STAGE_LABELS = {
    "create_requested": "Requesting GPU capacity",
    "allocated": "Booting instance & pulling image",
    "bootstrapping": "Preparing instance",
    "sanity_checking": "Downloading model & starting engine",
}


def replica_stage(replica: Any) -> str | None:
    """A serving replica's stage, with the setup steps of a replica without a pod in the console's words."""
    stage = getattr(replica, "current_stage", None)
    return SETUP_STAGE_LABELS.get(stage, stage) if stage else stage


def setup_stage_label(replica: Any) -> str | None:
    """The console's label when the replica is in one of the setup steps above, else None."""
    return SETUP_STAGE_LABELS.get(getattr(replica, "current_stage", None) or "")


def price_change(est: Any) -> str:
    """Sentence with the hourly price before and after an edit, from a pod or storage update estimate."""
    after = money.hourly(est.price_per_hour)
    if est.current_price_per_hour is None:
        return f" Its price will be {after}/hour."
    before = money.hourly(est.current_price_per_hour)
    return (f" Its price stays at {after}/hour." if before == after else
            f" Its price goes from {before}/hour to {after}/hour.")


# Arguments many tools share, described once. They fill an argument the tool doesn't describe itself, so that every
# argument's format is in the input schema, where clients look for it. The operation_id text replaces a paragraph that
# every write tool's description used to repeat; the server instructions hold the full rule.
SHARED_ARG_DOC = {
    "workspace": "Workspace id from the workspaces tool (a label also works).",
    "operation_id": ("Required for a write: the preview's operation_id, or a UUID made before the first call. Reuse it "
                     "for the confirmation and every retry; a new ID is a new operation. After a timeout, check "
                     "operation_status before retrying."),
    "confirm": "false (default) only previews and changes nothing. true does it: only after the user agreed.",
    "cursor": "`next_cursor` from the previous response. Omit it for the first page.",
    "limit": "Maximum number of items to return.",
}
# What a tool's `pod` or `storage` argument takes. A write that previews first accepts a display name only on the
# preview, so a renamed or deleted resource can't change the target of the confirmed call.
POD_ARG = "pod_name from the pods tool (the display name also works)."
POD_ARG_CONFIRM = ("pod_name from the pods tool. A display name works on the preview; confirm=true needs the returned "
                   "`pod_name`.")
STORAGE_ARG_CONFIRM = ("pv_name from the storages tool. A display name works on the preview; confirm=true needs the "
                       "returned pv_name.")
SERVING_ARG = "Serving id from the servings tool."
TASK_ARG = "task_id (task_...) from the tasks tool."
MACHINE_ARG = "machine_id from the machines tool."
# The operation_id of a write the server keeps no record of: it only ties the confirmation to its preview.
_UNRECORDED_OPERATION_ID = ("The preview's operation_id, or a UUID made before the first call. Reuse it for the "
                            "confirmation.")


def _tidy_schema(schema: Any, docs: dict[str, str]) -> None:
    """Fill the shared argument descriptions and drop the titles pydantic generates ("Operation Id"), which repeat
    each argument's name."""
    if not isinstance(schema, dict):
        return
    schema.pop("title", None)
    for name, sub in schema.get("properties", {}).items():
        if docs.get(name) and isinstance(sub, dict):
            sub.setdefault("description", docs[name])
        _tidy_schema(sub, {})
    for sub in schema.get("$defs", {}).values():
        _tidy_schema(sub, {})
    for key in ("items", "additionalProperties"):
        _tidy_schema(schema.get(key), {})
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for sub in schema.get(key, ()):
            _tidy_schema(sub, {})


_UNRECORDED_NOTE = ("\nThe server keeps no operation record of this call, so operation_status can't look it up and a "
                    "repeated {}call runs again.")
_RECORDED_RETRY = (" Check the resource/operation state first. Any retry MUST reuse this operation_id and the same "
                   "arguments; never submit a new operation for an unknown outcome.")
_UNRECORDED_RETRY = (" The server keeps no operation record of this call, so a retry runs it again: retry only if the "
                     "user still wants it.")
# Prepended to next_step when the server answered a write with its stored answer to an earlier request with the same
# operation_id: the write was not done again.
_REPLAYED_STEP = ("The server had already done this write for this operation_id (an earlier call, or an automatic "
                  "retry of this one) and returned that answer; nothing was done again. Use a new operation_id only "
                  "for a new action.")


# Added to a server_error (an unexpected failure on Meshive's side): the agent may offer the user a report, once.
_FEEDBACK_OFFER = (" If it still fails, you may ask the user once in this conversation whether to report it to Meshive "
                   "with send_feedback (tool \"{tool}\", error_code \"server_error\"); send it only if they agree.")


# Tools whose `confirm` is needed only for some calls (pausing a serving, locking a port, ...). The server instructions
# name each one as an exception to "every tool with a `confirm` argument needs it"; tests/test_instructions.py checks
# that the two lists match.
CONDITIONAL_CONFIRM: set[str] = set()


# Answers to a write that say for certain it was refused and nothing was done. The server releases the operation_id of
# such a refusal (it fails before any side effect), so the fix is what next_step says, not the same call again. 408 and
# 429 are left out (the same call is right later), and so are the idempotency refusals, which are about the
# operation_id itself.
_OPEN_OUTCOME_TITLES = {"idempotency-key reused", "operation outcome unknown", "request in progress"}


def _certain_refusal(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or not 400 <= status < 500 or status in (408, 429):
        return False
    return (getattr(exc, "title", None) or "").strip().lower() not in _OPEN_OUTCOME_TITLES


def _outcome_open(operation: dict) -> bool:
    """Whether a failed tool call may have left a write done or half done, so that the retry advice (same operation_id,
    same arguments, check state first) applies. Not when no write was sent (a preview, a local check, a missing
    operation_id), nor when the only write sent was refused for certain."""
    if not operation.get("sent"):
        return False
    return operation.get("completed", False) or not operation.get("refused", False)


def meshive_tool(server: MCPServer, name: str, *, title: str, annotations: ToolAnnotations,
                 operation_record: bool = True, conditional_confirm: bool = False,
                 arg_docs: dict[str, str] | None = None):
    """One layer over `server.tool`: (1) a normal result as compact JSON text + structured_content,
    (2) a ToolError raised by the tool as an `isError` result whose body is **pure JSON with no prefix**. The SDK's default prepends "Error executing tool <name>: ...",
    but the error body must be the `{code, message, next_step}` JSON itself — the model has to parse it
    and follow `next_step`, so there can't be a prefix.

    operation_record=False marks a write whose SDK call is in `_UNRECORDED`: its description and retry advice say
    that a repeated call runs again instead of pointing at operation_status. A tool that sends a recorded write too
    keeps the default; its failed unrecorded call still gets the unrecorded retry advice.

    conditional_confirm=True registers a tool whose `confirm` applies only to some calls in CONDITIONAL_CONFIRM.

    arg_docs describes the tool's arguments ({argument: description}) in its input schema. An argument annotated
    with its own `Field(description=...)` keeps that one, and SHARED_ARG_DOC covers the arguments many tools share.
    """
    if conditional_confirm:
        CONDITIONAL_CONFIRM.add(name)

    def decorator(fn: Callable[..., Awaitable[dict[str, Any]]]):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any):
            token = None
            op = None
            operation = {}
            try:
                bound = signature.bind(*args, **kwargs)
                oauth_call = (api_key(bound.arguments["ctx"]) or "").startswith("mshoa_") if "ctx" in bound.arguments else False
                if not annotations.read_only_hint:
                    supplied = bound.arguments.get("operation_id")
                    op = supplied or str(uuid.uuid4())
                    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", op):
                        raise tool_error("invalid_operation_id", "operation_id must be 8-128 characters of [A-Za-z0-9._:-].",
                                         "Use the operation_id returned by the preview, or generate a UUID before the first call.")
                    operation = {"id": op, "supplied": bool(supplied)}
                    token = _operation.set(operation)
                if (op and "confirm" in signature.parameters and name not in oauth.CONSOLE
                        and oauth_call):
                    result = await oauth_confirm.run(fn, name, bound, op)
                else:
                    result = await fn(*args, **kwargs)
            except oauth.ConsoleConfirmation as pending:
                return _bounded_result({**pending.result,
                    "next_step": "Open the Meshive console link to review the target and receive the result privately. "
                                 "Do not repeat the operation if the browser response is lost."})
            except ToolError as exc:
                try:
                    result = json.loads(str(exc))
                except (ValueError, TypeError):
                    result = {"code": "tool_error", "message": str(exc)}
                if op:
                    result["operation_id"] = op
                    if "lookup" in operation:
                        result["operation_lookup"] = operation["lookup"]
                    if result.get("code") not in _OWN_RETRY_ADVICE_CODES and _outcome_open(operation):
                        unrecorded = not operation_record or operation.get("unrecorded")
                        if unrecorded:
                            # operation_status can't find a call the server keeps no record of.
                            result["next_step"] = result.get("next_step", "").replace(UNAVAILABLE_WRITE_STEP, "")
                        result["next_step"] = (result.get("next_step", "") +
                                               (_UNRECORDED_RETRY if unrecorded else _RECORDED_RETRY))
                if result.get("code") == "server_error" and name != "send_feedback":
                    result["next_step"] = result.get("next_step", "") + _FEEDBACK_OFFER.format(tool=name)
                return _bounded_result(oauth.public_result(result) if oauth_call else result, is_error=True)
            finally:
                if token is not None:
                    _operation.reset(token)
            if op:
                result["operation_id"] = op
                if "lookup" in operation:
                    result["operation_lookup"] = operation["lookup"]
                if operation.get("replayed"):
                    result["replayed"] = True
                    result["next_step"] = " ".join(filter(None, (_REPLAYED_STEP, result.get("next_step"))))
                if result.get("confirmed") is False:
                    result["next_step"] += (" Reuse this operation_id for confirmation and every retry."
                                            if operation_record else " Reuse this operation_id for the confirmation.")
            return _bounded_result(oauth.public_result(result) if oauth_call else result)

        if not annotations.read_only_hint and not operation_record:
            takes_confirm = "confirm" in signature.parameters
            wrapper.__doc__ = (wrapper.__doc__ or "") + _UNRECORDED_NOTE.format("confirmed " if takes_confirm else "")
        # Several tools share the common annotation constants, so the title goes only on the copy. A result is any JSON
        # object, so an output schema would say nothing; the result still carries structured_content.
        server.tool(name=name, title=title, annotations=annotations.model_copy(update={"title": title}),
                    structured_output=False)(wrapper)
        unknown = set(arg_docs or ()) - set(signature.parameters)
        if unknown:
            raise ValueError(f"{name}: arg_docs names arguments the tool doesn't take: {sorted(unknown)}")
        docs = {**SHARED_ARG_DOC, **(arg_docs or {})}
        if annotations.read_only_hint:
            docs["operation_id"] = (arg_docs or {}).get("operation_id", "")  # not a write: the tool says what it is
        elif not operation_record:
            docs["operation_id"] = _UNRECORDED_OPERATION_ID
        _tidy_schema(server._tool_manager.get_tool(name).parameters, docs)
        return fn

    return decorator


async def call(fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
    """Wrap one SDK call and turn exceptions into a ToolError for the model."""
    name = getattr(fn, "__name__", "")
    is_write = name in _WRITES
    if name in _UNRECORDED:
        # A failure of this call gets the "a retry runs it again" advice, also in a tool whose other call is recorded.
        operation = _operation.get()
        if operation is not None:
            operation["unrecorded"] = True
    try:
        if is_write:
            operation = _operation.get()
            if operation is None or not operation["supplied"]:
                raise tool_error("operation_id_required", "No write was sent. A stable operation_id is required.",
                                 "Use the preview's operation_id or generate a UUID, then reuse it for every retry.")
            kwargs["idempotency_key"] = operation["id"]
        sends = is_write or name in _UNRECORDED
        operation = _operation.get()
        if sends and operation is not None:
            # From here on the write may reach the server; the wrapper's retry advice applies to a failure.
            operation["sent"] = True
            operation["refused"] = False
        result = await fn(*args, **kwargs)
        raw = getattr(result, "raw", {})
        operation = _operation.get()
        if sends and operation is not None:
            operation["completed"] = True
        if is_write and operation is not None and raw.get("operationMethod"):
            operation["lookup"] = {"method": raw["operationMethod"], "path": raw["operationPath"]}
        if is_write and operation is not None and getattr(result, "replayed", False) is True:
            operation["replayed"] = True
        return result
    except Exception as exc:  # noqa: BLE001 — exceptions translate doesn't know are re-raised
        operation = _operation.get()
        if is_write and operation is not None and getattr(exc, "operation_method", None):
            operation["lookup"] = {"method": exc.operation_method, "path": exc.operation_path}
        if operation is not None and operation.get("sent") and (_certain_refusal(exc) or type(exc) is ValueError):
            # The server refused it before doing anything (or the SDK did, before sending).
            operation["refused"] = True
        raise translate(exc) from exc
