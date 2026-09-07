"""GPU 가용량/가격. 이 도구만 API 키 없이도 동작한다(계약 §1, 유입 퍼널).

키 있음  → /v1/sdk/gpus : 실시간 재고 + 가격.
키 없음  → /v1/landing/pricing-catalog : 공개 가격표만. `availability: "unknown"` 으로 표시하고
           모델에게 재고를 보려면 키가 필요하다고 알린다.
"""
from __future__ import annotations

from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from meshive._config import resolve_base_url

from ..auth import api_key
from ..client import meshive_client
from ..errors import invalid_argument, translate
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
        items.append({
            "gpu_model": entry.get("model"),
            "vram_gb": vram,
            "rental_type": rental_type,
            "price_per_hour_usd": entry.get(price_key) or entry.get(_camel(price_key)),
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
    @meshive_tool(server, "gpus", annotations=READ_ONLY)
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
            row["price_per_hour_usd"] = row.pop("price_per_hour", None)
            rows.append(row)
        return {"items": rows, "total": len(rows), "authenticated": True}
