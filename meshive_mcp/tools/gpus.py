"""GPU availability/prices. The only tool that works without an API key.

With a key    → /v1/sdk/gpus : live stock + prices.
Without a key → /v1/landing/pricing-catalog : the public price list only. Marked `availability: "unknown"`, and
           the model is told a key is needed to see stock.
"""
from __future__ import annotations

from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from meshive._config import resolve_base_url

from ..auth import api_key
from ..client import meshive_client
from ..errors import invalid_argument, translate
from ..money import hourly
from ..serialize import to_dict
from ..settings import settings
from ._common import READ_ONLY, call, meshive_tool

RENTAL_TYPES = ("demand", "spot")


async def _public_catalog(rental_type: str, min_vram_gb: int | None) -> dict[str, Any]:
    base = resolve_base_url(settings.base_url).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as http:
            resp = await http.get(f"{base}/v1/landing/pricing-catalog")
            resp.raise_for_status()
            body = resp.json()
    except httpx.HTTPError as exc:
        raise translate(exc) from exc
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
