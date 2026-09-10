"""쓰기 도구 — 서빙·태스크 (write 스코프). 계약 §2.3, §2.4."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from .. import money
from ..client import meshive_client
from ..serialize import to_dict
from ..workspace import needs_input, resolve
from ._common import CREATE, DESTRUCTIVE, MUTATE, READ_ONLY, call, meshive_tool, preview


def register(server: MCPServer) -> None:
    # --- 서빙 -------------------------------------------------------------------

    @meshive_tool(server, "deploy_serving", annotations=CREATE)
    async def deploy_serving(ctx: Context[Any, Any], model_registration_id: int, price_cap_per_hour: float,
                             workspace: str | None = None, min_replicas: int = 1, max_replicas: int = 3,
                             autoscale: bool = True, max_context_tokens: int | None = None,
                             share_idle_capacity: bool = False, confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Deploy an already-registered model as a serverless serving (inference endpoint). Billing runs per replica per hour,
        capped at `price_cap_per_hour` each, so the worst case is price_cap × max_replicas per hour.
        With confirm=false (default) it only returns that cost summary; call again with confirm=true after the user agreed.
        Registering a model itself is done in the console, not here."""
        kwargs = dict(price_cap_per_hour=price_cap_per_hour, min_replicas=min_replicas, max_replicas=max_replicas,
                      autoscale=autoscale, max_context_tokens=max_context_tokens, share_idle_capacity=share_idle_capacity)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                ceiling = Decimal(str(price_cap_per_hour)) * max_replicas
                return preview("deploy_serving", {"workspace": ws, "model_registration_id": model_registration_id,
                                                  "replicas": {"min": min_replicas, "max": max_replicas, "autoscale": autoscale},
                                                  "price_cap_per_hour_usd": str(price_cap_per_hour),
                                                  "max_hourly_cost_usd": format(ceiling.normalize(), "f")},
                               f"Deploy model #{model_registration_id} with up to {max_replicas} replica(s) at most "
                               f"{money.hourly(price_cap_per_hour)}/hour each "
                               f"(≤ {money.hourly(ceiling)}/hour total)?")
            result = await call(client.deploy_serving, model_registration_id, workspace=ws, **kwargs)
        out = to_dict(result)
        out["next_step"] = "Deployment was accepted. Poll the servings tool until status is `active`; it returns the endpoint URL."
        return out

    @meshive_tool(server, "scale_serving", annotations=MUTATE)
    async def scale_serving(ctx: Context[Any, Any], serving: int, min_replicas: int | None = None,
                            max_replicas: int | None = None, autoscale: bool | None = None,
                            price_cap_per_hour: float | None = None, confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Change a serving's replica range, autoscaling, or per-replica price cap. Pass only the fields to change.
        Any change that can raise the hourly cost — a larger replica range, turning autoscale on, or a higher per-replica
        price cap — with confirm=false (default) only returns the current state and the requested change and changes
        nothing; call again with confirm=true after the user agreed. Lowering the range, turning autoscale off, or
        lowering the cap applies immediately."""
        requested = {k: v for k, v in dict(min_replicas=min_replicas, max_replicas=max_replicas, autoscale=autoscale,
                                            price_cap_per_hour=price_cap_per_hour).items() if v is not None}
        async with meshive_client(ctx) as client:
            if not confirm:
                # 비용이 늘 수 있는 변경만 확인 — 판정은 SDK `Serving.scale_raises_cost` (CLI 와 같은 규칙, 리뷰 P2 #6).
                current = await call(client.get_serving, serving)
                if current.scale_raises_cost(**requested):
                    new_min = current.min_replicas if min_replicas is None else min_replicas
                    new_max = current.max_replicas if max_replicas is None else max_replicas
                    cap = (f", cap {money.hourly(price_cap_per_hour)}/hour per replica" if price_cap_per_hour is not None
                           else "")
                    return preview("scale_serving", {"serving": to_dict(current), "requested": requested},
                                   f"Scale serving #{serving} from {current.min_replicas}-{current.max_replicas} to "
                                   f"{new_min}-{new_max} replicas{cap}"
                                   f"{', autoscale on' if autoscale else ''}? Its possible hourly cost goes up.")
            result = await call(client.scale_serving, serving, min_replicas=min_replicas, max_replicas=max_replicas,
                                autoscale=autoscale, price_cap_per_hour=price_cap_per_hour)
        out = to_dict(result)
        out["next_step"] = "Accepted. Poll the servings tool to see the new replica counts."
        return out

    @meshive_tool(server, "pause_serving", annotations=MUTATE)
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

    @meshive_tool(server, "delete_serving", annotations=DESTRUCTIVE)
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

    # --- 태스크 -------------------------------------------------------------------

    _TASK_DOC = """`script` is Python source (max 256 KB) run inside `image` (or a `template_id`); use print(..., flush=True) so output
reaches the logs. Pass exactly one of `gpu_model` (GPU task, optional `gpu_count`/`gpu_vram_gb`) or `cpu_preset`
(e.g. "micro-2c8g", "small-4c16g", "standard-8c32g"). `max_duration` seconds (3600..86400) is a hard stop.
`max_price_per_hour` caps the final compute rate only; storage, Asset Hub retention and fetch-time charges are separate.
The estimate is not a total-bill ceiling. `input_assets` attaches Asset Hub assets as ["asset_id" or {"asset": id, "version": n}] under /inputs."""

    async def estimate_task(ctx: Context[Any, Any], name: str, script: str, workspace: str | None = None,
                            image: str | None = None, template_id: int | None = None, requirements: str | None = None,
                            env: dict[str, str] | None = None, secret_keys: list[str] | None = None,
                            args: list[str] | None = None, gpu_model: str | None = None, gpu_count: int | None = None,
                            gpu_vram_gb: int | None = None, cpu_preset: str | None = None, max_duration: int = 3600,
                            webhook_url: str | None = None, input_assets: list[Any] | None = None,
                            max_price_per_hour: float | None = None) -> dict[str, Any]:
        """Estimate a one-off task's compute hourly price (total cost can be unknown) before submitting it; nothing runs.
        """
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            est = await call(client.estimate_task, name, script, workspace=ws, image=image, template_id=template_id,
                             requirements=requirements, env=env, secret_keys=secret_keys, args=args, gpu_model=gpu_model,
                             gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, cpu_preset=cpu_preset, max_duration=max_duration,
                             webhook_url=webhook_url, input_assets=input_assets, max_price_per_hour=max_price_per_hour)
        out = to_dict(est)
        out["workspace"] = ws
        out["next_step"] = "Show the price and max cost to the user, then submit_task with confirm=true if they agree."
        return out

    estimate_task.__doc__ = (estimate_task.__doc__ or "") + "\n" + _TASK_DOC
    meshive_tool(server, "estimate_task", annotations=READ_ONLY)(estimate_task)

    async def submit_task(ctx: Context[Any, Any], name: str, script: str, workspace: str | None = None,
                          image: str | None = None, template_id: int | None = None, requirements: str | None = None,
                          env: dict[str, str] | None = None, secret_keys: list[str] | None = None,
                          args: list[str] | None = None, gpu_model: str | None = None, gpu_count: int | None = None,
                          gpu_vram_gb: int | None = None, cpu_preset: str | None = None, max_duration: int = 3600,
                          webhook_url: str | None = None, input_assets: list[Any] | None = None,
                          max_price_per_hour: float | None = None, confirm: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        """Submit a one-off task (a script that runs to completion on a GPU or CPU). It is billed while it runs, up to
        `max_duration`. With confirm=false (default) it only returns the estimate; call again with confirm=true after the
        user agreed. Then poll the tasks tool for status and read output with logs.
        """
        kwargs = dict(image=image, template_id=template_id, requirements=requirements, env=env, secret_keys=secret_keys,
                      args=args, gpu_model=gpu_model, gpu_count=gpu_count, gpu_vram_gb=gpu_vram_gb, cpu_preset=cpu_preset,
                      max_duration=max_duration, webhook_url=webhook_url, input_assets=input_assets,
                      max_price_per_hour=max_price_per_hour)
        async with meshive_client(ctx) as client:
            ws = await resolve(client, workspace)
            if needs_input(ws):
                return ws
            if not confirm:
                est = await call(client.estimate_task, name, script, workspace=ws, **kwargs)
                cost = (f"up to {money.usd(est.max_cost)}" if est.max_cost is not None
                        else "an amount that depends on the machine")
                return preview("submit_task", {"estimate": to_dict(est), "workspace": ws, "name": name},
                               f"Submit task '{name}' ({cost} for at most {max_duration}s)?")
            submitted = await call(client.submit_task, name, script, workspace=ws, **kwargs)
        out = to_dict(submitted)
        out["next_step"] = (f"Task {submitted.task.task_id} is queued. Poll the tasks tool for status (queued → running → "
                            "succeeded/failed) and use logs with task=... for its output.")
        return out

    submit_task.__doc__ = (submit_task.__doc__ or "") + "\n" + _TASK_DOC
    meshive_tool(server, "submit_task", annotations=CREATE)(submit_task)

    @meshive_tool(server, "stop_task", annotations=MUTATE)
    async def stop_task(ctx: Context[Any, Any], task: str, operation_id: str | None = None) -> dict[str, Any]:
        """Stop a queued or running task. `task` is the task_id (task_...) from the tasks tool. Billing stops."""
        async with meshive_client(ctx) as client:
            result = await call(client.stop_task, task)
        out = to_dict(result)
        out["next_step"] = "Accepted. Poll the tasks tool until status is `stopped`."
        return out
