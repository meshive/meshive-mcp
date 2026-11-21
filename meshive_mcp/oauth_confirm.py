"""Bind OAuth write confirmations to the previewed arguments and resolved resources."""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import json

import httpx
from pydantic_core import to_jsonable_python

from . import oauth
from .auth import api_key
from .errors import tool_error


def digest(value):
    encoded = json.dumps(to_jsonable_python(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class Resolutions:
    def __init__(self, inputs, pinned=None):
        self.inputs = inputs
        self.pinned = pinned
        self.bindings = {}
        self.canonical = dict(inputs)


resolutions: ContextVar[Resolutions | None] = ContextVar("meshive_preview_resolutions", default=None)


def resolved(field, *, single_item_list=False):
    def decorator(fn):
        signature = inspect.signature(fn)
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            capture = resolutions.get()
            if capture is None:
                return await fn(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            params = {k: v for k, v in bound.arguments.items() if k not in {"client", "confirm"}}
            key = digest([fn.__name__, params])
            if capture.pinned is not None and key in capture.pinned:
                pinned = capture.pinned[key]
                if single_item_list:
                    # Preserve the list's current role and label metadata, using the ID the user approved.
                    bound.arguments[field] = pinned
                    return await fn(*bound.args, **bound.kwargs)
                return pinned
            result = await fn(*args, **kwargs)
            target = result[0] if single_item_list and isinstance(result, list) and len(result) == 1 else result
            if isinstance(target, str):
                capture.bindings[key] = target
                if field in capture.inputs and capture.inputs[field] == params[field]:
                    capture.canonical[field] = target
            return result
        return wrapper
    return decorator


async def receipt(token, decision, data):
    try:
        async with oauth.private_http() as http:
            response = await http.post("/internal/oauth/previews/" + decision,
                headers={"Authorization": "Bearer " + token}, json=data)
            if response.status_code == 409:
                raise tool_error("preview_required", "This confirmation does not match a current preview.",
                                 "Ask for a new preview with confirm=false and a new operation_id. Show it to the user.")
            response.raise_for_status()
            return response.json()["bindings"]
    except (httpx.HTTPError, OSError, ValueError, KeyError):
        raise tool_error("confirmation_unavailable", "Could not verify the preview.", "Check operation_status before retrying.") from None


async def run(fn, name, bound, op):
    bound.apply_defaults()
    inputs = {k: v for k, v in bound.arguments.items() if k not in {"ctx", "confirm", "operation_id"}}
    token = api_key(bound.arguments["ctx"])
    confirmed = bound.arguments["confirm"]
    data = {"tool": name, "operation_id": op, "input_hash": digest(inputs)}
    pins = await receipt(token, "check", data) if confirmed else None
    captured = Resolutions(inputs, pins)
    active = resolutions.set(captured)
    try:
        result = await fn(*bound.args, **bound.kwargs)
        if not confirmed and result.get("confirmed") is False:
            await receipt(token, "save", {**data, "canonical_hash": digest(captured.canonical), "bindings": captured.bindings})
        return result
    finally:
        resolutions.reset(active)
