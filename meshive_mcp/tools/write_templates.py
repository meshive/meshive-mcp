"""Write tools — custom pod templates of a workspace (write scope): save_template and delete_template.

Saving a template costs nothing (pods made from it go through create_pod's confirm), so save_template has no
`confirm`; deleting one can't be undone, so delete_template previews first.
"""
from __future__ import annotations

import json
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from ..client import meshive_client
from ..errors import invalid_argument, tool_error
from ..serialize import to_dict
from ..workspace import needs_input, resolve_many
from ._common import DESTRUCTIVE, MUTATE, call, meshive_tool, preview
from .templates import template_call

_REPLACES = ("read templates(template=id) first (its items can be passed back as they are); this list replaces the "
             "stored one")
# The item keys each list argument takes; anything else is refused before a call, so a misspelled flag (a secret
# marked "is_secret") can't slip through as something else.
_ENV_KEYS = {"key", "value", "secret", "auto_generate", "show_on_connect", "from_image"}
_PORT_KEYS = {"port", "name", "external", "locked"}
_MOUNT_KEYS = {"path", "name"}
_SEMANTIC_KEYS = {"role", "path", "expect", "include", "required", "cardinality", "placement"}
# The lists templates(template=id) returns use a few other key names: renamed to the write name, or dropped where
# they only describe the stored item (whether a value is stored, the Connect port, the port's row id).
_READ_RENAMES = {"env": {"auto_generated": "auto_generate"}, "volume_mounts": {"mount_path": "path"}}
_READ_ONLY_KEYS = {"env": {"value_set"}, "ports": {"connect", "template_port_id"}}
# What save_template returns of the saved template besides the template itself.
_SAVE_FIELDS = ("workspace", "action", "removed_envs", "removed_ports", "replayed")
_OFFICIAL_STEP = ("Official templates can't be changed or deleted; nothing was changed. Don't retry. To change its "
                  "settings, create a custom template (save_template without template_id) if the user wants one.")


def _official(template_id: int) -> ToolError:
    return tool_error("official_template", f"Template {template_id} is an official template.", _OFFICIAL_STEP)


async def _custom_call(client: Any, template_id: int, ws: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """A change of a custom template. The server answers an official template's id like a missing one (not found);
    the template is read then, so that case says it is official instead of "check the id"."""
    try:
        return await template_call(fn, *args, **kwargs)
    except ToolError as exc:
        try:
            code = json.loads(str(exc)).get("code")
        except ValueError:
            raise exc from None
        if code != "not_found":
            raise
        try:
            template = await call(client.get_template, template_id, ws)
        except ToolError:
            raise exc from None
        if template.is_official:
            raise _official(template_id) from exc.__cause__
        raise

_Env = Annotated[list[dict[str, Any]] | None, Field(description=(
    '[{"key": "HF_HOME", "value": "/data/hf"}] with optional "secret", "auto_generate", "show_on_connect" and '
    '"from_image" (booleans; "from_image" marks a variable the image declares). A secret takes no "value": "auto_generate": true gives each pod a random one; otherwise it is '
    "given at create_pod time, or the value the template stores is kept. " + _REPLACES + "."))]
_Ports = Annotated[list[dict[str, Any]] | None, Field(description=(
    '[{"port": 8888, "name": "jupyter", "external": true}] with optional "locked" for a public port; '
    + _REPLACES + "."))]
_Mounts = Annotated[list[dict[str, Any]] | None, Field(description=(
    'Folders that should be on a volume: [{"path": "/workspace", "name": "work"}]; ' + _REPLACES + "."))]
_SemanticPaths = Annotated[list[dict[str, Any]] | None, Field(description=(
    'Input folders for assets and folders whose files become assets: [{"role": "model", "path": "/models"}] with '
    'optional "expect", "include", "required", "cardinality", "placement"; [] clears them; ' + _REPLACES + "."))]


def _from_read_shape(items: list[dict[str, Any]] | None, arg: str) -> list[dict[str, Any]] | None:
    """The items with the read-side key names of templates(template=id) turned into the ones this tool takes, so the
    lists can be passed back as they were read."""
    if items is None:
        return None
    renames, dropped = _READ_RENAMES.get(arg, {}), _READ_ONLY_KEYS.get(arg, set())
    out: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        item = {k: v for k, v in item.items() if k not in dropped}
        for read_key, write_key in renames.items():
            if read_key not in item:
                continue
            value = item.pop(read_key)
            if write_key in item and item[write_key] != value:
                raise invalid_argument(f"A `{arg}` item has both \"{read_key}\" and \"{write_key}\" with different "
                                       f"values; pass only \"{write_key}\".")
            item[write_key] = value
        if arg == "env" and item.get("secret") is True and item.get("value") in (None, ""):
            # A secret without a value keeps the stored one; an empty string would overwrite it with "".
            item.pop("value", None)
        out.append(item)
    return out


def _check_items(items: list[dict[str, Any]] | None, arg: str, allowed: set[str], unique: str | None) -> None:
    if items is None:
        return
    seen: set[Any] = set()
    for item in items:
        if not isinstance(item, dict):
            raise invalid_argument(f"`{arg}` items must be objects.")
        extra = sorted(set(item) - allowed)
        if extra:
            raise invalid_argument(f"`{arg}` items take only {', '.join(sorted(allowed))}; not {', '.join(extra)}.")
        if unique is None:
            continue
        value = item.get(unique)
        if value is None:
            raise invalid_argument(f"Every `{arg}` item needs \"{unique}\".")
        if value in seen:
            raise invalid_argument(f"`{arg}` has \"{unique}\" {value!r} twice.")
        seen.add(value)


def _check_secrets(env: list[dict[str, Any]] | None) -> None:
    for item in env or []:
        value = item.get("value")
        if item.get("secret") is True and value is not None and str(value) != "":
            raise tool_error("secret_value_not_accepted", f"The secret variable '{item.get('key')}' has a value.",
                             "Nothing was saved. Leave the secret's value out: with auto_generate each pod gets a "
                             "random value; otherwise the value is given at create_pod time (env + secret_keys) or "
                             "typed in the console. Never ask the user to paste a secret here.",
                             key=item.get("key"))


def _ports_by_access(template: Any) -> tuple[list[int], list[int]]:
    """The public ports saved locked, and those saved open (an undecided one is locked by create_pod by default)."""
    public = [p for p in template.ports if p.external]
    return [p.port for p in public if p.locked is True], [p.port for p in public if p.locked is False]


def register(server: MCPServer) -> None:
    @meshive_tool(server, "save_template", title="Save Custom Template", annotations=MUTATE, arg_docs={
        "description": "The template's description.",
    })
    async def save_template(
            ctx: Context[Any, Any],
            template_id: Annotated[int | None, Field(description=(
                "A custom template to change, from the templates tool. Omit it to create one."))] = None,
            workspace: str | None = None,
            name: Annotated[str | None, Field(description=(
                "Unique among the workspace's custom templates; required to create."))] = None,
            image: Annotated[str | None, Field(description=(
                "The container image; required to create and can't be changed later."))] = None,
            description: str | None = None,
            command: Annotated[str | None, Field(description=(
                'Replaces the image\'s entrypoint and cmd; "" uses the image\'s own.'))] = None,
            env: _Env = None,
            ports: _Ports = None,
            volume_mounts: _Mounts = None,
            semantic_paths: _SemanticPaths = None,
            registry_login_id: Annotated[int | None, Field(description=(
                "A `registry_login_id` from source_credentials for a private image; 0 removes it."))] = None,
            lock_public_ports: Annotated[bool, Field(description=(
                'Public ports without their own "locked" are saved locked.'))] = True,
            operation_id: str | None = None) -> dict[str, Any]:
        """Create a custom pod template in a workspace, or change one (`template_id` from the templates tool), like the console's Templates > Add/Edit. Use it when the user wants pods from an image no official template offers. It needs the admin or billing role and costs nothing; pods made from it are created and billed through create_pod.
        To create, pass `name` and `image`; the image is read first (see templates `image`) and nothing is saved if it can't be read. A private image needs `registry_login_id` from source_credentials. The image can't be changed later.
        To change, pass only what changes. `env`, `ports`, `volume_mounts` and `semantic_paths` each replace the whole list when given, so start from the template's current lists (templates with `template`); the answer lists what was removed. Pods already created keep their settings.
        `ports`: "external": true gives the port a public HTTPS URL. Public ports are saved locked (only signed-in members and the port's access keys get in) unless the user explicitly wants one open ("locked": false); the answer lists `locked_ports` and `open_ports`.
        Secrets: never put a secret's value here. Mark the variable "secret": true with "auto_generate": true or leave the value out to give it at create time; a secret without a value keeps the value the template already stores."""
        env = _from_read_shape(env, "env")
        ports = _from_read_shape(ports, "ports")
        volume_mounts = _from_read_shape(volume_mounts, "volume_mounts")
        _check_items(env, "env", _ENV_KEYS, "key")
        _check_items(ports, "ports", _PORT_KEYS, "port")
        _check_items(volume_mounts, "volume_mounts", _MOUNT_KEYS, "path")
        _check_items(semantic_paths, "semantic_paths", _SEMANTIC_KEYS, None)
        _check_secrets(env)
        creating = template_id is None
        lists = {"envs": env, "ports": ports, "volume_mounts": volume_mounts, "semantic_paths": semantic_paths}
        if creating:
            if not (name or "").strip() or not (image or "").strip():
                raise invalid_argument("To create a template pass `name` and `image`; to change one pass "
                                       "`template_id`.")
        else:
            if image is not None:
                raise tool_error("image_change_not_supported", "A template's image can't be changed.",
                                 "Nothing was saved. Leave `image` out, or create a new template with the other image "
                                 "(save_template without template_id) if the user wants that.")
            if all(v is None for v in (name, description, command, registry_login_id, *lists.values())):
                raise invalid_argument("Pass at least one thing to change besides `template_id`.")
        given = {k: v for k, v in lists.items() if v is not None}
        async with meshive_client(ctx) as client:
            targets = await resolve_many(client, workspace, allow_all=False)
            if needs_input(targets):
                return targets
            ws = targets[0]
            if creating:
                login = registry_login_id or None
                await template_call(client.inspect_image, image.strip(), workspace=ws, registry_login_id=login)
                saved = await template_call(client.create_template, name.strip(), image.strip(), workspace=ws,
                                            description=description or "", command=command or "",
                                            registry_login_id=login, lock_public_ports=lock_public_ports, **given)
            else:
                changes: dict[str, Any] = {k: v for k, v in (("name", name), ("description", description),
                                                             ("command", command)) if v is not None}
                if registry_login_id is not None:
                    changes["registry_login_id"] = registry_login_id or None
                saved = await _custom_call(client, template_id, ws, client.update_template, template_id, ws,
                                           lock_public_ports=lock_public_ports, **changes, **given)
        template = to_dict(saved)
        for key in _SAVE_FIELDS:
            template.pop(key, None)
        locked, open_ports = _ports_by_access(saved)
        out: dict[str, Any] = {"created": creating, "template": template, "workspace": ws,
                               "workspace_name": targets.names.get(ws), "locked_ports": locked,
                               "open_ports": open_ports, "removed_envs": list(saved.removed_envs),
                               "removed_ports": list(saved.removed_ports)}
        out["next_step"] = (f"Saved as template_id {saved.template_id}. Use it as template_id in "
                            "estimate_pod/create_pod." if creating else
                            "Saved. Pods created from now on use it; existing pods keep their settings.")
        if open_ports:
            out["next_step"] += (" Ports in open_ports are open to anyone with the URL on every pod made from this "
                                 "template unless locked at create time; tell the user.")
        return out

    @meshive_tool(server, "delete_template", title="Delete Custom Template", annotations=DESTRUCTIVE)
    async def delete_template(ctx: Context[Any, Any],
                              template_id: Annotated[int, Field(description=(
                                  "A custom template's `template_id` from the templates tool."))],
                              workspace: str | None = None, confirm: bool = False,
                              operation_id: str | None = None) -> dict[str, Any]:
        """Delete a custom pod template of a workspace (`template_id` from the templates tool). It needs the admin or billing role and can't be undone; official templates can't be deleted. Pods already created from it keep running, restarting and billing with the settings they were created with.
        With confirm=false (default) it only returns the template and the pods made from it; call again with confirm=true after the user agreed."""
        async with meshive_client(ctx) as client:
            targets = await resolve_many(client, workspace, allow_all=False)
            if needs_input(targets):
                return targets
            ws = targets[0]
            if not confirm:
                template = await call(client.get_template, template_id, ws)
                if template.is_official:
                    raise _official(template_id)
                pods = [p for p in await call(client.list_pods, ws)
                        if p.template_id == template_id and not getattr(p, "raw", {}).get("isDownloader")]
                using = [{"pod_name": p.pod_name, "user_alias": p.user_alias, "status": p.status} for p in pods]
                question = (f"Delete the custom template '{template.name}' ({template.image})? It can't be undone. "
                            f"{len(using)} pod(s) were created from it; they keep running with their settings, but "
                            "nobody can create new pods from it.")
                return preview("delete_template", {
                    "template": {"template_id": template.template_id, "name": template.name, "image": template.image,
                                 "port_count": len(template.ports), "env_count": len(template.envs)},
                    "workspace": ws, "workspace_name": targets.names.get(ws), "pods_using_it": using}, question)
            result = await _custom_call(client, template_id, ws, client.delete_template, template_id, ws)
        return {"resource": "template", "id": result.id or str(template_id), "workspace": result.workspace or ws,
                "action": "delete", "next_step": "Deleted."}
