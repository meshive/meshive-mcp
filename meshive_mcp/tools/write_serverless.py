"""Write tools — serving and tasks (write scope)."""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from meshive import SHARING_CONSENT
from meshive.exceptions import ConflictError, NotFoundError
from pydantic import Field

from .. import money
from ..client import meshive_client
from ..errors import invalid_argument, serving_write_error, tool_error
from ..serialize import to_dict
from ..workspace import needs_input, resolve, resolve_target
from ._common import CREATE, DESTRUCTIVE, MUTATE, READ_ONLY, call, meshive_tool, preview
from .servings import _unsupported
from .tasks import _FINISHED, _SAVING_PHASES, _outputs

# The input_assets mapping keys the task body carries ("asset_id" is an alias of "asset"; "version" is ignored with a
# deprecation warning by the SDK; "paths" and "strip_prefix" pick files, as in create_pod).
_INPUT_ASSET_KEYS = frozenset({"asset", "asset_id", "target_dir", "paths", "strip_prefix", "version"})
# What the API sends in place of a secret env value; it can't be submitted back.
_MASKED_VALUE = "***"
# The output upload phases a task's upload can still be stopped in.
_CANCELLABLE_PHASES = frozenset({"preparing", "uploading", "retrying"})
MAX_DELETE_TASKS = 20
# Refusals of the availability check that the deploy itself would get too: they fail the preview instead of being
# shown as a missing availability.
_NOT_A_PROBE_FAILURE = frozenset({"no_api_key", "invalid_api_key", "unknown_workspace"})

_OutputBucket = Annotated[int | Literal["managed"] | None, Field(description=(
    'Where outputs go: a bucket_id from buckets, or "managed" for Meshive storage.'))]
_DELETE_EFFECT = ("Output assets and billing history stay. The task disappears from lists, details and logs, and its "
                  "script, requirements, env and args are erased; this can't be undone.")


def _replicas_kept(current: Any, min_replicas: int | None, max_replicas: int | None, autoscale: bool | None) -> int:
    """How many replicas a serving keeps once a scale change is applied. The API removes running replicas above
    this number: with autoscale off it holds `min_replicas`, with autoscale on it allows up to `max_replicas`,
    and it never goes below one (pausing is what stops them all)."""
    auto = current.autoscale if autoscale is None else autoscale
    new_min = current.min_replicas if min_replicas is None else min_replicas
    new_max = current.max_replicas if max_replicas is None else max_replicas
    return max(max(new_max, new_min) if auto else new_min, 1)


async def _serving_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """`call` with the serving write tools' own reading of the server's refusals (sharing_not_available, ...)."""
    try:
        return await call(fn, *args, **kwargs)
    except ToolError as exc:
        mapped = serving_write_error(exc.__cause__)
        if mapped is None:
            raise
        raise mapped from exc.__cause__


def _sharing(rolling_restart: bool) -> dict[str, Any]:
    """What the user agrees to when idle-capacity sharing is turned on, as the console's consent dialog lists it."""
    return {"consent": list(SHARING_CONSENT), "rolling_restart": rolling_restart}


def _consent_required(sharing: dict[str, Any], what: str) -> ToolError:
    return tool_error("sharing_consent_required",
                      f"Turning on idle-capacity sharing for {what} needs the user's agreement to its terms.",
                      "Nothing was sent. Show the user the terms in `sharing.consent`. Only after they agree, call "
                      "again with sharing_consent=true and confirm=true.", sharing=sharing)


def _check_bucket_arg(output_bucket: Any) -> None:
    if isinstance(output_bucket, bool) or (isinstance(output_bucket, int) and output_bucket <= 0):
        raise invalid_argument('`output_bucket` is a bucket_id from buckets, or "managed".')


async def _sharing_refusal(client: Any, ws: str | None, registration_id: int | None, what: str) -> None:
    """Refuse turning sharing on for a model that can't share idle capacity, before the user is asked to agree to
    terms the server would then refuse ("Sharing Not Available"). The model is looked up in the workspace's own
    registrations, then in the official catalog; a model neither list shows is left to the server."""
    if not ws or registration_id is None:
        return
    model = None
    for read in (lambda: call(client.list_models, ws), lambda: call(client.list_official_models)):
        try:
            models = await read()
        except ToolError:
            continue
        model = next((m for m in models if m.registration_id == registration_id), None)
        if model is not None:
            break
    if model is not None and not model.can_share_idle_capacity:
        raise tool_error("sharing_not_available",
                         f"{what} can't share idle capacity: sharing is only for official text models served with "
                         "vLLM.",
                         "Nothing was changed. Leave share_idle_capacity out (deploy or scale without sharing); "
                         "don't ask the user to agree to the sharing terms for this model.")


async def _bucket(client: Any, ws: str, bucket_id: int) -> Any:
    """The connected bucket with this id in the workspace (its label goes in a preview)."""
    found = next((c for c in await call(client.list_storage_credentials, ws) if c.credential_id == bucket_id), None)
    if found is None:
        raise invalid_argument(f"No connected bucket with bucket_id {bucket_id} in workspace {ws}. Use a bucket_id "
                               "from buckets.")
    return found


def _bucket_dict(found: Any) -> dict[str, Any]:
    return {"bucket_id": found.credential_id, "label": found.label, "bucket": found.bucket}


async def _deploy_availability(client: Any, registration_id: int, ws: str, cap: float,
                               max_context_tokens: int | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Best-effort: which GPUs that fit the model are free at or under the cap. A failed check (a viewer, an older
    server) leaves the preview without it."""
    try:
        found = await call(client.get_model_availability, registration_id, workspace=ws,
                           max_context_tokens=max_context_tokens)
    except ToolError as exc:
        try:
            body = json.loads(str(exc))
        except ValueError:
            body = {}
        if body.get("code") in _NOT_A_PROBE_FAILURE:
            raise
        return None, {"code": body.get("code", "unknown"), "message": body.get("message", "")}
    fits = found.within_cap(cap)
    cheapest = fits[0].price_per_hour if fits else None
    suggested = found.suggested_cap_per_hour
    return {"deployable": found.deployable, "status": found.status, "gpus_within_cap": len(fits),
            "cheapest_within_cap_per_hour": cheapest,
            "cheapest_within_cap_per_hour_display": None if cheapest is None else money.hourly(cheapest),
            "suggested_price_cap_per_hour": suggested,
            "suggested_price_cap_per_hour_display": None if suggested is None else money.hourly(suggested)}, None


def _check_task_args(script: str | None, project: dict[str, Any] | None, template_id: int | None,
                     registry_login_id: int | None, env: dict[str, str] | None, input_assets: list[Any] | None,
                     output_bucket: Any) -> None:
    """Refusals made before anything is sent."""
    if (script is None) == (project is None):
        raise invalid_argument("Pass exactly one of `script` or `project`.")
    if registry_login_id is not None and template_id is not None:
        raise invalid_argument("`registry_login_id` is for a custom `image`; a `template_id` brings its own registry "
                               "login.")
    for key, value in (env or {}).items():
        if value == _MASKED_VALUE:
            raise tool_error("masked_secret_value", f"The value of {key} was hidden by the API and can't be reused.",
                             "Ask the user to re-run it from the console, where Re-run keeps secret values, or "
                             "submit without that variable.", variable=key)
    _check_input_assets(input_assets)
    _check_bucket_arg(output_bucket)


def _check_input_assets(input_assets: list[Any] | None) -> None:
    """Refuse input_assets keys the task body doesn't carry (the SDK would refuse them too, in its own words)."""
    for item in input_assets or []:
        if isinstance(item, dict):
            extra = sorted(set(item) - _INPUT_ASSET_KEYS)
            if extra:
                raise invalid_argument(
                    f"input_assets entries take `asset`, `target_dir`, `paths` and `strip_prefix`; "
                    f"{', '.join(extra)} isn't one of them. Nothing was submitted.", unsupported=extra)


async def _task_echo(client: Any, ws: str, image: str | None, template_id: int | None,
                     registry_login_id: int | None, project: dict[str, Any] | None,
                     input_assets: list[Any] | None, output_bucket: Any) -> dict[str, Any]:
    """How a task is set up, as a preview repeats it to the user."""
    if template_id is not None:
        source = f"template #{template_id}"
    elif registry_login_id is not None:
        source = f"image {image} (registry login #{registry_login_id})"
    else:
        source = f"image {image}"
    if output_bucket is None:
        bucket: Any = "workspace default"
    elif output_bucket == "managed":
        bucket = "Meshive storage"
    else:
        bucket = f"bucket {(await _bucket(client, ws, output_bucket)).label}"
    inputs = []
    for item in input_assets or []:
        if isinstance(item, dict):
            row = {"asset": item.get("asset") or item.get("asset_id"), "target_dir": item.get("target_dir"),
                   "paths": item.get("paths"), "strip_prefix": item.get("strip_prefix")}
            inputs.append({k: v for k, v in row.items() if v is not None})
        else:
            inputs.append({"asset": item})
    return {"image_source": source, "output_bucket": bucket, "project": project, "input_assets": inputs}


def register(server: MCPServer) -> None:
    # --- Serving -------------------------------------------------------------------

    @meshive_tool(server, "deploy_serving", title="Deploy Serving", annotations=CREATE)
    async def deploy_serving(ctx: Context[Any, Any], model_registration_id: int, price_cap_per_hour: float,
                             workspace: str | None = None, min_replicas: int = 1, max_replicas: int = 3,
                             autoscale: bool = True, max_context_tokens: int | None = None,
                             share_idle_capacity: bool = False,
                             output_bucket: Annotated[int | Literal["managed"] | None, Field(description=(
                                 "A bucket_id from buckets for an image or video model's outputs; omit it or pass "
                                 '"managed" for Meshive storage.'))] = None,
                             sharing_consent: bool = False,
                             confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Deploy an already-registered model as a serverless serving (inference endpoint). Billing runs per replica per hour,
        capped at `price_cap_per_hour` each, so the worst case is price_cap × max_replicas per hour.
        With confirm=false (default) it only returns that cost summary; call again with confirm=true after the user agreed.
        The summary also shows `availability`: how many GPUs that fit the model are free at or under the cap now
        (`gpus_within_cap`) and the suggested cap; with none, replicas wait until one is.
        Find `model_registration_id` with the models tool, or register a Hugging Face model with register_model first.
        share_idle_capacity=true lets other workspaces use the serving's idle capacity; it also needs
        sharing_consent=true, which you may set only after the user agreed to the terms the summary lists."""
        _check_bucket_arg(output_bucket)
        if output_bucket == "managed":
            # Meshive storage is what a deploy without a bucket uses.
            output_bucket = None
        kwargs = dict(price_cap_per_hour=price_cap_per_hour, min_replicas=min_replicas, max_replicas=max_replicas,
                      autoscale=autoscale, max_context_tokens=max_context_tokens, share_idle_capacity=share_idle_capacity,
                      output_bucket_id=output_bucket)
        sharing = _sharing(False) if share_idle_capacity else None
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                ceiling = Decimal(str(price_cap_per_hour)) * max_replicas
                payload: dict[str, Any] = {"workspace": ws, "model_registration_id": model_registration_id,
                                           "replicas": {"min": min_replicas, "max": max_replicas,
                                                        "autoscale": autoscale},
                                           "price_cap_per_hour_usd": str(price_cap_per_hour),
                                           "max_hourly_cost_usd": format(ceiling.normalize(), "f")}
                question = (f"Deploy model #{model_registration_id} with up to {max_replicas} replica(s) at most "
                            f"{money.hourly(price_cap_per_hour)}/hour each "
                            f"(≤ {money.hourly(ceiling)}/hour total)?")
                if output_bucket is not None:
                    payload["output_bucket"] = _bucket_dict(await _bucket(client, ws, output_bucket))
                availability, failure = await _deploy_availability(client, model_registration_id, ws,
                                                                    price_cap_per_hour, max_context_tokens)
                payload["availability"] = availability
                if failure is not None:
                    payload["availability_error"] = failure
                elif availability["status"] != "error" and availability["gpus_within_cap"] == 0:
                    suggested = availability["suggested_price_cap_per_hour_display"]
                    question += (" No GPU that fits this model is free at or under this cap right now; replicas wait "
                                 "until one is" + (f" (suggested cap: {suggested}/hour)." if suggested else "."))
                if sharing is not None:
                    await _sharing_refusal(client, ws, model_registration_id, f"Model #{model_registration_id}")
                    payload["sharing"] = sharing
                    payload["sharing_consent_required"] = not sharing_consent
                    question += " Idle-capacity sharing is turned on: " + " ".join(SHARING_CONSENT)
                return preview("deploy_serving", payload, question)
            if sharing is not None and not sharing_consent:
                raise _consent_required(sharing, f"model #{model_registration_id}")
            result = await _serving_call(client.deploy_serving, model_registration_id, workspace=ws, **kwargs)
        out = to_dict(result)
        out["next_step"] = "Deployment was accepted. Poll the servings tool until status is `active`; it returns the endpoint URL."
        return out

    @meshive_tool(server, "register_model", title="Register Model", annotations=CREATE)
    async def register_model(ctx: Context[Any, Any], huggingface_repo: str, workspace: str | None = None,
                             name: str | None = None, framework: str | None = None, hf_token_id: int | None = None,
                             context_length: int | None = None,
                             takes_image: Annotated[bool | None, Field(description=(
                                 "Whether the model takes image input as well as text. Omit it to let Hugging Face "
                                 "say."))] = None,
                             operation_id: str | None = None) -> dict[str, Any]:
        """Register a Hugging Face model for serving and return its `registration_id` for deploy_serving. It costs nothing:
        the model downloads only when deployed. Run detect_model first; registering the same repo again returns the
        existing registration. Leave `framework` out: text models run on vllm, and sglang is refused. `hf_token_id`
        (from source_credentials) is for private repos. Left out, `framework` and the input types are read from the
        Hugging Face repo, as in the console; when the repo doesn't say, it answers modality_required."""
        modalities = None if takes_image is None else (["text", "image"] if takes_image else ["text"])
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            result = await _serving_call(client.register_model, huggingface_repo, workspace=ws, name=name,
                                         framework=framework, hf_token_id=hf_token_id, context_length=context_length,
                                         input_modalities=modalities)
        out = to_dict(result)
        out["registration_id"] = int(result.id) if result.id.isdigit() else result.id
        out["next_step"] = ("Registered. To serve it, call deploy_serving with this registration_id and a per-replica "
                            "price cap, after showing the user the cost.")
        return out

    @meshive_tool(server, "delete_model", title="Delete Model", annotations=DESTRUCTIVE)
    async def delete_model(ctx: Context[Any, Any], registration_id: int, workspace: str | None = None,
                           confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Delete a model registration. It fails while a serving of that model is still deployed.
        With confirm=false (default) it only returns the registration; call again with confirm=true after the user agreed."""
        async with meshive_client(ctx) as client:
            if not confirm:
                ws = await resolve(client, workspace)
                if needs_input(ws):
                    return ws
                current = next((m for m in await call(client.list_models, ws) if m.registration_id == registration_id),
                               None)
                if current is None:
                    raise invalid_argument(f"Model registration #{registration_id} is not in workspace {ws}.")
                return preview("delete_model", {"model": to_dict(current), "workspace": ws},
                               f"Delete model registration #{registration_id} ({current.name or current.huggingface_repo})?")
            result = await call(client.delete_model, registration_id)
        out = to_dict(result)
        out["next_step"] = "The registration was deleted."
        return out

    @meshive_tool(server, "scale_serving", title="Scale Serving", annotations=MUTATE,
                  conditional_confirm=True)
    async def scale_serving(ctx: Context[Any, Any], serving: int, min_replicas: int | None = None,
                            max_replicas: int | None = None, autoscale: bool | None = None,
                            price_cap_per_hour: float | None = None, output_bucket: _OutputBucket = None,
                            share_idle_capacity: bool | None = None, sharing_consent: bool = False,
                            confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Change a serving's replica range, autoscaling, or per-replica price cap. Pass only the fields to change.
        Any change that can raise the hourly cost — a larger replica range, turning autoscale on, or a higher per-replica
        price cap — with confirm=false (default) only returns the current state and the requested change and changes
        nothing; call again with confirm=true after the user agreed. Lowering the range or turning autoscale off
        applies immediately: replicas beyond the new range are removed within about 30 seconds and the requests they
        are handling fail — with autoscale off the serving keeps `min_replicas`, with autoscale on up to
        `max_replicas`. When that happens the answer has `replicas_removed`; if the serving is in use, tell the user
        before lowering it. Lowering the cap only applies to replicas started afterwards: running replicas are neither
        moved nor removed, and they keep the GPU and the hourly rate they started with, which can be above the new
        cap.
        `output_bucket` sets where an image or video serving stores its outputs from now on (a bucket_id from buckets,
        or "managed"); no confirmation needed. `share_idle_capacity=true` lets other workspaces use this serving's idle
        capacity (official text models on vLLM only) and credits you what guests pay; turning it on needs confirm and
        `sharing_consent=true` after the user agreed to the terms the preview lists. Turning it off applies at once.
        Only the member who deployed the serving can change it."""
        _check_bucket_arg(output_bucket)
        requested = {k: v for k, v in dict(min_replicas=min_replicas, max_replicas=max_replicas, autoscale=autoscale,
                                            price_cap_per_hour=price_cap_per_hour).items() if v is not None}
        changes = {k: v for k, v in dict(output_bucket=output_bucket,
                                          share_idle_capacity=share_idle_capacity).items() if v is not None}
        async with meshive_client(ctx) as client:
            try:
                current = await call(client.get_serving, serving)
            except ToolError as exc:
                # A serving reads back only for the member who deployed it, so a 404 here is that, or a wrong id.
                if not isinstance(exc.__cause__, NotFoundError) or _unsupported(exc.__cause__):
                    raise
                raise tool_error("not_serving_owner", f"No serving #{serving} that you deployed was found.",
                                 "Nothing was changed. Only the member who deployed a serving can change it, so "
                                 "either another member deployed it or the id is wrong. Check the id with the "
                                 "servings tool; if another member deployed it, tell the user to ask them. Don't "
                                 "guess another id.") from exc.__cause__
            # Confirm only changes that can raise cost — decided by the SDK's `Serving.scale_raises_cost` (same rule as
            # the CLI) — and turning sharing on, which the user agrees to first.
            raises_cost = current.scale_raises_cost(**requested)
            sharing_on = current.sharing_needs_consent(share_idle_capacity)
            sharing = _sharing(current.sharing_state != "ready") if sharing_on else None
            if not confirm and sharing_on:
                await _sharing_refusal(client, current.namespace_name, current.registration_id,
                                       f"Serving #{serving}'s model")
            if not confirm and (raises_cost or sharing_on):
                questions = []
                if raises_cost:
                    new_min = current.min_replicas if min_replicas is None else min_replicas
                    new_max = current.max_replicas if max_replicas is None else max_replicas
                    cap = (f", cap {money.hourly(price_cap_per_hour)}/hour per replica" if price_cap_per_hour is not None
                           else "")
                    questions.append(f"Scale serving #{serving} from {current.min_replicas}-{current.max_replicas} to "
                                     f"{new_min}-{new_max} replicas{cap}"
                                     f"{', autoscale on' if autoscale else ''}? Its possible hourly cost goes up.")
                payload: dict[str, Any] = {"serving": to_dict(current), "requested": {**requested, **changes}}
                if sharing is not None:
                    restart = ("Its replicas restart one by one to turn it on." if sharing["rolling_restart"]
                               else "Nothing restarts.")
                    questions.append(f"Turn on idle-capacity sharing for serving #{serving}? "
                                     f"{' '.join(SHARING_CONSENT)} {restart}")
                    payload["sharing"] = sharing
                    payload["sharing_consent_required"] = not sharing_consent
                return preview("scale_serving", payload, " ".join(questions))
            if sharing is not None and not sharing_consent:
                raise _consent_required(sharing, f"serving #{serving}")
            result = await _serving_call(
                client.scale_serving, serving, min_replicas=min_replicas, max_replicas=max_replicas,
                autoscale=autoscale, price_cap_per_hour=price_cap_per_hour,
                output_bucket_id=output_bucket if isinstance(output_bucket, int) else None,
                managed_output=output_bucket == "managed", share_idle_capacity=share_idle_capacity)
        out = to_dict(result)
        running = current.current_replicas or 0
        kept = _replicas_kept(current, min_replicas, max_replicas, autoscale)
        # Only a change of the range or of autoscale removes replicas; a cap, bucket or sharing change keeps them.
        resized = any(v is not None for v in (min_replicas, max_replicas, autoscale))
        if resized and running > kept:
            out["replicas_removed"] = running - kept
            out["next_step"] = (f"Accepted. {running - kept} of {running} running replicas are being removed (the serving "
                                f"keeps {kept}); requests they were handling fail. Tell the user, then poll the servings "
                                "tool to see the new replica counts.")
        elif requested:
            out["next_step"] = "Accepted. Poll the servings tool to see the new replica counts."
        else:
            out["next_step"] = "Accepted."
        if (price_cap_per_hour is not None and current.price_cap_per_hour is not None
                and Decimal(str(price_cap_per_hour)) < Decimal(str(current.price_cap_per_hour))):
            # A lower cap only filters where new replicas go; running replicas keep their GPU and stored rate.
            out["next_step"] += (" The lower price cap applies to replicas started from now on; running replicas keep "
                                 "their GPUs and the hourly rates they started with, so tell the user the cost may stay "
                                 "above the new cap until they stop.")
        if output_bucket is not None:
            out["next_step"] += " The output bucket applies to outputs created from now on."
        if share_idle_capacity is True:
            if sharing is None:
                out["next_step"] += " Sharing was already on."
            elif sharing["rolling_restart"]:
                out["next_step"] += (" Its replicas restart one by one; poll servings: sharing_state is pending until "
                                     "the restart finishes, then ready.")
            else:
                out["next_step"] += " Sharing is on again; guests are routed to it now and nothing restarts."
        elif share_idle_capacity is False:
            out["next_step"] += " Sharing is off: guests stop being routed to it; nothing restarts."
        return out

    @meshive_tool(server, "pause_serving", title="Pause or Resume Serving", annotations=MUTATE,
                  conditional_confirm=True)
    async def pause_serving(ctx: Context[Any, Any], serving: int, paused: bool = True,
                            confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Pause (paused=true) or resume (paused=false) a serving. Paused servings stop billing and stop answering requests.
        Resuming restarts billing, so paused=false with confirm=false (default) only returns the serving's current state;
        call again with confirm=true after the user agreed. Pausing needs no confirmation."""
        async with meshive_client(ctx) as client:
            if not paused and not confirm:
                current = await call(client.get_serving, serving)
                return preview("pause_serving", {"serving": to_dict(current), "paused": False},
                               f"Resume serving #{serving} ({current.model_name or current.framework}, "
                               f"{current.min_replicas}-{current.max_replicas} replicas)? Billing resumes.")
            result = await call(client.pause_serving, serving, paused=paused)
        out = to_dict(result)
        out["next_step"] = "Accepted. Poll the servings tool for the new state."
        return out

    @meshive_tool(server, "delete_serving", title="Delete Serving", annotations=DESTRUCTIVE)
    async def delete_serving(ctx: Context[Any, Any], serving: int, confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Delete a serving and its endpoint. With confirm=false (default) it only returns the serving's current state;
        call again with confirm=true after the user agreed."""
        async with meshive_client(ctx) as client:
            if not confirm:
                current = await call(client.get_serving, serving)
                return preview("delete_serving", {"serving": to_dict(current)},
                               f"Delete serving #{serving} ({current.model_name or current.framework}, "
                               f"{current.current_replicas} replica(s))? Its endpoint stops working.")
            result = await call(client.delete_serving, serving)
        out = to_dict(result)
        out["next_step"] = "Deletion was accepted."
        return out

    # --- Tasks -------------------------------------------------------------------

    _TASK_DOC = """`script` is Python source (max 256 KiB) run inside `image` (or a `template_id`); use print(..., flush=True) so output
reaches the logs. Pass exactly one of `script` or `project` ({"asset": an uploaded folder asset, "entrypoint":
"train.py", "requirements_path": "requirements.txt"}; `requirements` then adds extra packages). A private `image` needs
`registry_login_id`; a `template_id` brings its own. Pass exactly one of `gpu_model` (GPU task, optional
`gpu_count`/`gpu_vram_gb`; gpus with rental_type "task" lists them) or `cpu_preset`
(e.g. "micro-2c8g", "small-4c16g", "standard-8c32g"). `max_duration` seconds (3600..86400) is a hard stop.
`max_price_per_hour` caps the final compute rate only; storage, Asset Hub retention and fetch-time charges are separate.
The estimate is not a total-bill ceiling. `input_assets` attaches Asset Hub assets as ["asset_id" or {"asset": id, "target_dir": dir}] under /inputs;
an item can pick files with "paths" and "strip_prefix", as in create_pod.
`output_bucket` sets where /outputs are saved: omit for the workspace's default, "managed" for Meshive storage, or a
bucket_id from buckets.
`output_include` uploads only the /outputs files matching these globs, relative to /outputs and case-sensitive:
["*.safetensors"] matches at any depth, ["samples/*.png"] only in that folder, ["checkpoints/"] every folder of that name
with its contents; omitted = every file. Files left out still use /outputs disk while the task runs.
A task's `output_include` is null when no filter was given; if you passed `output_include` and the submitted task's is
null, the server ignores the filter and uploads every file.
To re-run a task, copy its settings from tasks(task=...); a secret env value can't be copied."""

    async def estimate_task(ctx: Context[Any, Any], name: str, script: str | None = None, workspace: str | None = None,
                            image: str | None = None, template_id: int | None = None, requirements: str | None = None,
                            env: dict[str, str] | None = None, secret_keys: list[str] | None = None,
                            args: list[str] | None = None, gpu_model: str | None = None, gpu_count: int | None = None,
                            gpu_vram_gb: int | None = None, cpu_preset: str | None = None, max_duration: int = 3600,
                            webhook_url: str | None = None, input_assets: list[Any] | None = None,
                            max_price_per_hour: float | None = None,
                            output_include: list[str] | None = None, project: dict[str, Any] | None = None,
                            registry_login_id: int | None = None,
                            output_bucket: _OutputBucket = None) -> dict[str, Any]:
        """Estimate a one-off task's compute hourly price (total cost can be unknown) before submitting it; nothing runs.
        A `project`'s files are checked when the task is submitted, not here.
        """
        _check_task_args(script, project, template_id, registry_login_id, env, input_assets, output_bucket)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            echo = await _task_echo(client, ws, image, template_id, registry_login_id, project, input_assets,
                                    output_bucket)
            est = await call(client.estimate_task, name, script, workspace=ws, image=image, template_id=template_id,
                             requirements=requirements, env=env, secret_keys=secret_keys, args=args, gpu_model=gpu_model,
                             gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, cpu_preset=cpu_preset, max_duration=max_duration,
                             webhook_url=webhook_url, input_assets=input_assets, max_price_per_hour=max_price_per_hour,
                             output_include=output_include, project=project, registry_login_id=registry_login_id,
                             output_bucket=output_bucket)
        out = to_dict(est)
        out["workspace"] = ws
        out.update(echo)
        out["next_step"] = "Show the price and max cost to the user, then submit_task with confirm=true if they agree."
        return out

    estimate_task.__doc__ = (estimate_task.__doc__ or "") + "\n" + _TASK_DOC
    meshive_tool(server, "estimate_task", title="Estimate Task Price", annotations=READ_ONLY)(estimate_task)

    async def submit_task(ctx: Context[Any, Any], name: str, script: str | None = None, workspace: str | None = None,
                          image: str | None = None, template_id: int | None = None, requirements: str | None = None,
                          env: dict[str, str] | None = None, secret_keys: list[str] | None = None,
                          args: list[str] | None = None, gpu_model: str | None = None, gpu_count: int | None = None,
                          gpu_vram_gb: int | None = None, cpu_preset: str | None = None, max_duration: int = 3600,
                          webhook_url: str | None = None, input_assets: list[Any] | None = None,
                          max_price_per_hour: float | None = None, output_include: list[str] | None = None,
                          project: dict[str, Any] | None = None, registry_login_id: int | None = None,
                          output_bucket: _OutputBucket = None,
                          confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Submit a one-off task (a script that runs to completion on a GPU or CPU). It is billed while it runs, up to
        `max_duration`. With confirm=false (default) it only returns the estimate; call again with confirm=true after the
        user agreed. Then poll the tasks tool for status and read output with logs.
        """
        _check_task_args(script, project, template_id, registry_login_id, env, input_assets, output_bucket)
        kwargs = dict(image=image, template_id=template_id, requirements=requirements, env=env, secret_keys=secret_keys,
                      args=args, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, cpu_preset=cpu_preset,
                      max_duration=max_duration, webhook_url=webhook_url, input_assets=input_assets,
                      max_price_per_hour=max_price_per_hour, output_include=output_include, project=project,
                      registry_login_id=registry_login_id, output_bucket=output_bucket)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                echo = await _task_echo(client, ws, image, template_id, registry_login_id, project, input_assets,
                                        output_bucket)
                est = await call(client.estimate_task, name, script, workspace=ws, **kwargs)
                cost = (f"up to {money.usd(est.max_cost)}" if est.max_cost is not None
                        else "an amount that depends on the machine")
                return preview("submit_task", {"estimate": to_dict(est), "workspace": ws, "name": name, **echo},
                               f"Submit task '{name}' ({cost} for at most {max_duration}s)?")
            submitted = await call(client.submit_task, name, script, workspace=ws, **kwargs)
        out = to_dict(submitted)
        out["next_step"] = (f"Task {submitted.task.task_id} is queued. Poll the tasks tool for status (queued → scheduling → "
                            "running → succeeded/failed) and use logs with task=... for its output.")
        return out

    submit_task.__doc__ = (submit_task.__doc__ or "") + "\n" + _TASK_DOC
    meshive_tool(server, "submit_task", title="Submit Task", annotations=CREATE)(submit_task)

    @meshive_tool(server, "stop_task", title="Stop Task", annotations=MUTATE)
    async def stop_task(ctx: Context[Any, Any], task: str, operation_id: str | None = None) -> dict[str, Any]:
        """Stop a queued or running task. `task` is the task_id (task_...) from the tasks tool. Billing stops."""
        async with meshive_client(ctx) as client:
            result = await call(client.stop_task, task)
        out = to_dict(result)
        out["next_step"] = "Accepted. Poll the tasks tool until status is `stopped`."
        return out

    @meshive_tool(server, "delete_tasks", title="Delete Finished Tasks", annotations=DESTRUCTIVE)
    async def delete_tasks(ctx: Context[Any, Any],
                           tasks: Annotated[list[str], Field(description=(
                               'Up to 20 task_id values ("task_...") from the tasks tool.'))],
                           workspace: str | None = None, confirm: bool = False,
                           operation_id: str | None = None) -> dict[str, Any]:
        """Delete finished tasks (up to 20 per call). Output assets and billing history stay; the task disappears from
        lists, details and logs, and its script, requirements, env and args are erased. This can't be undone.
        A task still running is skipped (stop it with stop_task first), and so is one whose outputs are still being
        saved (wait, or cancel_task_upload). With confirm=false (default) it only lists what would happen to each task
        (`will`: delete, skip_running, skip_uploading or not_found) and deletes nothing; call again with the workspace
        id and confirm=true after the user agreed (a label is refused there). Needs the admin or billing role.
        `workspace` takes the id from the workspaces tool (a label also works for the preview); omit it if the user
        has only one."""
        ids = list(dict.fromkeys(t.strip() for t in tasks if t and t.strip()))
        if not ids or len(ids) > MAX_DELETE_TASKS:
            raise invalid_argument(f"Pass 1 to {MAX_DELETE_TASKS} task_id values.")
        async with meshive_client(ctx) as client:
            if confirm:
                ws = await resolve_target(client, workspace or "", confirm=True)
                result = await call(client.delete_tasks, ws, ids)
            else:
                ws = await resolve(client, workspace)
                if needs_input(ws):
                    return ws
                gate = asyncio.Semaphore(5)

                async def read(task_id: str) -> Any:
                    async with gate:
                        try:
                            return await call(client.get_task, task_id)
                        except ToolError as exc:
                            if isinstance(exc.__cause__, NotFoundError):
                                return None
                            raise

                found = await asyncio.gather(*(read(t) for t in ids))
                items = []
                for task_id, task in zip(ids, found):
                    if task is None or task.namespace_name != ws:
                        items.append({"task_id": task_id, "will": "not_found"})
                        continue
                    outputs = _outputs(task)
                    if task.status not in _FINISHED:
                        will = "skip_running"
                    elif outputs is not None and outputs["phase"] in _SAVING_PHASES:
                        will = "skip_uploading"
                    else:
                        will = "delete"
                    items.append({"task_id": task_id, "name": task.name, "status": task.status,
                                  "finished_at": to_dict(task.finished_at),
                                  "outputs_summary": outputs["summary"] if outputs else None, "will": will})
                count = sum(1 for i in items if i["will"] == "delete")
                out = preview("delete_tasks", {"workspace": ws, "items": items, "effect": _DELETE_EFFECT},
                              f"Delete {count} finished task(s)? {_DELETE_EFFECT}")
                out["next_step"] += f" Set `workspace` to {ws} for the confirmation."
                return out
        skipped = [{"task_id": s.task_id, "reason": s.reason, "message": s.message} for s in result.skipped]
        steps = [f"Deleted {len(result.deleted)} task(s)."]
        by_reason: dict[str, list[str]] = {}
        for s in result.skipped:
            by_reason.setdefault(s.reason, []).append(s.task_id)
        advice = {"active": "{} still run: stop them with stop_task first if the user wants them deleted.",
                  "finalizing": "{} just ended: try again in a minute.",
                  "uploading": "{} are still saving outputs: wait, or cancel_task_upload if the user wants to stop "
                               "that.",
                  "not_found": "{} weren't found in this workspace."}
        for reason, task_ids in by_reason.items():
            steps.append(advice.get(reason, "{} were skipped: see `skipped`.").format(", ".join(task_ids)))
        return {"deleted": result.deleted, "skipped": skipped, "workspace": ws, "next_step": " ".join(steps)}

    @meshive_tool(server, "cancel_task_upload", title="Cancel Task Output Upload", annotations=DESTRUCTIVE)
    async def cancel_task_upload(ctx: Context[Any, Any], task: str, confirm: bool = False,
                                 operation_id: str | None = None) -> dict[str, Any]:
        """Stop saving the rest of a finished task's /outputs files while they upload. Files already uploaded stay and
        become an asset; the rest are lost, and the task's /outputs disk stops being billed. It works while the task's
        `outputs.phase` (tasks tool) is preparing, uploading or retrying. `task` is the task_id (task_...).
        With confirm=false (default) it only shows how far the upload got; call again with confirm=true after the user
        agreed. Needs the admin or billing role."""
        nothing = ("Nothing was changed. Check the task with the tasks tool: its outputs aren't uploading, so there is "
                   "nothing to cancel.")
        async with meshive_client(ctx) as client:
            if not confirm:
                current = await call(client.get_task, task)
                outputs = _outputs(current)
                phase = outputs["phase"] if outputs else None
                if phase not in _CANCELLABLE_PHASES:
                    raise tool_error("nothing_to_cancel", f"Task {task} has no output upload in progress "
                                     f"(outputs.phase is {phase or 'unknown'}).", nothing)
                progress = {k: outputs[k] for k in ("phase", "summary", "files_so_far", "files_to_upload",
                                                    "bytes_so_far", "bytes_total")}
                return preview("cancel_task_upload", {"task_id": current.task_id, "name": current.name,
                                                      "outputs": progress},
                               "Stop saving the rest of this task's outputs? Files already uploaded stay and become an "
                               "asset; the rest are lost, and the task's /outputs disk stops being billed.")
            try:
                result = await call(client.discard_task_outputs, task)
            except ToolError as exc:
                cause = exc.__cause__
                detail = cause.raw.get("detail") if isinstance(cause, ConflictError) and isinstance(cause.raw, dict) else None
                if isinstance(detail, dict) and detail.get("code") == "upload_not_active":
                    raise tool_error("nothing_to_cancel", cause.message or "The upload isn't active.",
                                     nothing) from cause
                raise
        out = to_dict(result)
        out["next_step"] = ("Accepted: the upload stops within about 30 seconds. Poll tasks(task=...) until "
                            "outputs.phase is discarded.")
        return out
