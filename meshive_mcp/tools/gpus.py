"""GPU availability/prices. The only tool that works without an API key.

With a key    → /v1/sdk/gpus : live stock + prices.
Without a key → /v1/landing/pricing-catalog : the public price list only. Marked `availability: "unknown"`, and
           the model is told a key is needed to see stock. One copy, shared by every caller (see CATALOG_TTL).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from meshive._config import resolve_base_url

from ..auth import api_key
from ..client import meshive_client
from ..errors import NEXT_STEP_UNAVAILABLE, ToolError, invalid_argument, tool_error
from ..money import hourly
from ..serialize import to_dict
from ..settings import settings
from ._common import READ_ONLY, call, meshive_tool

RENTAL_TYPES = ("demand", "spot")
# The price list changes rarely. Without the copy every keyless call is a request to the API, which anyone can send
# without an account, and on a slow API those calls hold the server's request slots.
CATALOG_TTL = 60.0       # A younger copy is used as it is.
CATALOG_MAX_AGE = 600.0  # A copy up to this old is used while a refresh is out, or after one failed.
RETRY_DELAY = 5.0        # How long after a failed refresh the next one can start.

_catalog: tuple[float, dict[str, Any]] | None = None  # (time.monotonic() when fetched, body)
_refresh: asyncio.Task[dict[str, Any]] | None = None
_failed_at = float("-inf")

logger = logging.getLogger(__name__)


async def _fetch_catalog() -> dict[str, Any]:
    global _catalog
    base = resolve_base_url(settings.base_url).rstrip("/")
    async with httpx.AsyncClient(timeout=settings.request_timeout) as http:
        # httpx's timeout is for each connect and each read, so a response sent a byte at a time never runs out. Every
        # caller without a copy waits on this request, so it gets one deadline as a whole.
        resp = await asyncio.wait_for(http.get(f"{base}/v1/landing/pricing-catalog"), settings.request_timeout)
        resp.raise_for_status()
        body = resp.json()
    if not isinstance(body, dict):
        # Kept, it would break every keyless call until the next refresh.
        raise ValueError(f"The price list is a JSON {type(body).__name__}, not an object.")
    _catalog = (time.monotonic(), body)
    return body


def _settled(task: asyncio.Task[dict[str, Any]]) -> None:
    # Here rather than in the task, so it also runs for a task that ends before its first step.
    global _refresh, _failed_at
    if _refresh is task:
        _refresh = None
    # Reading the exception also keeps a refresh nobody waited for from being logged as "never retrieved".
    if not task.cancelled() and task.exception() is not None:
        _failed_at = time.monotonic()
        logger.warning("Couldn't refresh the public GPU price list: %r", task.exception())


def _unavailable() -> ToolError:
    return tool_error("temporarily_unavailable", "The Meshive API didn't return the GPU price list.",
                      NEXT_STEP_UNAVAILABLE.format(retry_after=15), retry_after=15)


async def _catalog_body() -> dict[str, Any]:
    """One request to the API at a time, none for RETRY_DELAY after a failure. Callers without a usable copy wait for
    the request that's out."""
    global _refresh
    now = time.monotonic()
    age = now - _catalog[0] if _catalog else float("inf")
    if _catalog and age < CATALOG_TTL:
        return _catalog[1]
    if _refresh is None and now - _failed_at >= RETRY_DELAY:
        _refresh = asyncio.create_task(_fetch_catalog())
        _refresh.add_done_callback(_settled)
    if _catalog and age < CATALOG_MAX_AGE:
        return _catalog[1]
    if _refresh is None:
        raise _unavailable()
    try:
        # Shielded: a caller that goes away doesn't cancel the request the others wait for.
        return await asyncio.shield(_refresh)
    except Exception as exc:  # noqa: BLE001 - every failure is the same to the caller, and _settled logs it.
        raise _unavailable() from exc


async def _public_catalog(rental_type: str, min_vram_gb: int | None) -> dict[str, Any]:
    body = await _catalog_body()
    price_key = "demand_price" if rental_type == "demand" else "spot_price"
    items = []
    for entry in body.get("gpu_pod_prices") or body.get("gpuPodPrices") or []:
        vram = int(entry.get("vram") or 0)
        if min_vram_gb and vram < min_vram_gb:
            continue
        price = entry.get(price_key) or entry.get(_camel(price_key))
        items.append({
            "gpu_model": entry.get("model"),
            "vram_gb": vram,
            "rental_type": rental_type,
            "price_per_hour": price,
            # This path doesn't go through to_dict — without attaching display directly, only keyless agents would round
            # on their own and quote amounts that differ from the console (the very first number someone without an account sees).
            "price_per_hour_display": hourly(price),
            "availability": "unknown",
        })
    return {
        "items": items,
        "total": len(items),
        "authenticated": False,
        "note": "Prices only. Live availability needs a Meshive API key in the Authorization header.",
    }


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(p.title() for p in rest)


def register(server: MCPServer) -> None:
    @meshive_tool(server, "gpus", title="List GPUs", annotations=READ_ONLY)
    async def gpus(ctx: Context[Any, Any], rental_type: str = "demand",
                   min_vram_gb: int | None = None) -> dict[str, Any]:
        """Show which GPU types can be rented right now, with per-GPU hourly prices in USD.
        Works without an API key (prices only); with a key it also shows live availability and max GPUs per pod.
        Use `rental_type` "spot" for the cheaper preemptible tier and `min_vram_gb` to filter by VRAM."""
        rental_type = (rental_type or "demand").lower()
        if rental_type not in RENTAL_TYPES:
            raise invalid_argument("rental_type must be 'demand' or 'spot'.", allowed=list(RENTAL_TYPES))
        if not api_key(ctx):
            return await _public_catalog(rental_type, min_vram_gb)
        async with meshive_client(ctx) as client:
            items = await call(client.list_gpus, rental_type=rental_type, min_vram=min_vram_gb)
        rows = []
        for g in items:
            row = to_dict(g)
            row["vram_gb"] = row.pop("vram", None)
            rows.append(row)
        return {"items": rows, "total": len(rows), "authenticated": True}
