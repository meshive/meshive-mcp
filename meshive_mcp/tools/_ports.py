"""Port access wording shared by the pod tools.

A port a pod exposes over HTTPS is locked or unlocked. A locked port lets through only workspace members signed in to
the console and requests that carry one of the port's access keys; everything else gets 401. An agent that meets that
401 must not take it for a broken pod, so every place that shows a locked port says what the lock is and how to get
through it.

A new port is locked unless someone decided otherwise. That someone is the user (lock_public_ports=false, or
"locked": false on a port) or the author of the template, who can set one of its ports to open. The two are told
apart here, because only the first is something the user asked for.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

# How to call the app without going through the lock, and whose decision an access key is.
REACH_IT = ("To call the app for your own work, use run_command inside the pod against http://localhost:<port>, which "
            "the lock doesn't cover. For a program outside the pod, ask the user whether to create an access key "
            "(create_access_key); never create or use one on your own.")
# Returned with every pod that has a locked port.
ACCESS_NOTE = ("An endpoint whose `access` is locked lets through only workspace members signed in to the console and "
               "requests that carry one of the port's access keys. A request without a key gets 401, and a browser is "
               "sent to the console to sign in: that is the lock working, not a fault, so don't restart, edit or "
               "delete the pod for it. " + REACH_IT + " `unlocked` means anyone who knows the URL gets in.")
# What unlocking, or creating a port open, means. Said in every preview that leaves a port open.
OPEN_MEANS = "anyone who knows the URL can reach the app, with no sign-in and no access key"
_CONNECT = ("To connect, a workspace member opens the port's URL in a browser and signs in to the console, and a "
            "program sends one of the port's access keys (create_access_key) as `Authorization: Bearer <key>`; any "
            "other request gets 401, which is the lock working, not a fault.")


@dataclass(frozen=True)
class Locks:
    """The lock each exposed port of a pod request gets, as the SDK sent it or would send it."""

    locked: list[int]
    opened: list[int]           # every port left open
    by_template: list[int]      # those of `opened` that the request didn't ask to open: the template sets them open

    @property
    def asked(self) -> list[int]:
        """The open ports the request itself asked for."""
        return [port for port in self.opened if port not in self.by_template]


def _port(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _asked_open(ports: Any, template_ports: Any) -> set[int]:
    """The final port numbers whose item in `ports` or `template_ports` says "locked": false."""
    asked: set[int] = set()
    for item in list(ports or []) + list(template_ports or []):
        if isinstance(item, dict) and item.get("locked") is False:
            number = _port(item.get("new_port", item.get("port")))
            if number is not None:
                asked.add(number)
    return asked


def read(port_locks: Any, *, lock_public_ports: bool = True, ports: Any = None, template_ports: Any = None) -> Locks:
    """Sort the SDK's {port: locked} into locked and open ports, given the arguments the request was made with. A port
    left open although lock_public_ports is true and no item said "locked": false is open by the template's setting."""
    locks = dict(port_locks or {})
    locked = sorted(port for port, value in locks.items() if value is True)
    opened = sorted(port for port, value in locks.items() if value is not True)
    asked = set(opened) if not lock_public_ports else _asked_open(ports, template_ports)
    return Locks(locked, opened, [port for port in opened if port not in asked])


def summary(locks: Locks) -> dict[str, list[int]]:
    """`locked_ports` and `open_ports` for a preview or result, and `open_by_template` when the template opens some;
    nothing when the request sets no lock."""
    if not locks.locked and not locks.opened:
        return {}
    out = {"locked_ports": locks.locked, "open_ports": locks.opened}
    if locks.by_template:
        out["open_by_template"] = locks.by_template
    return out


def _numbers(ports: Iterable[int]) -> str:
    """"8188", "3000 and 8188", "3000, 8080 and 8188"."""
    numbers = [str(port) for port in ports]
    return numbers[0] if len(numbers) == 1 else f"{', '.join(numbers[:-1])} and {numbers[-1]}"


def ports_text(ports: Iterable[int]) -> str:
    """"port 8188", "ports 3000 and 8188", "ports 3000, 8080 and 8188"."""
    ports = list(ports)
    return f"port{'s' if len(ports) > 1 else ''} {_numbers(ports)}"


def lock_items(ports: Iterable[int]) -> str:
    """The `template_ports` value that locks these ports, as the agent would pass it."""
    return json.dumps([{"port": port, "locked": True} for port in ports])


def preview_note(locks: Locks, *, creating: bool) -> str:
    """Sentences for a create or edit preview about the lock each exposed port would get, or "" when none is set."""
    note = ""
    if locks.locked:
        many = len(locks.locked) > 1
        if creating:
            note += (f" Its {len(locks.locked)} public port{'s' if many else ''} ({_numbers(locks.locked)}) "
                     f"{'are' if many else 'is'} created locked. ")
        else:
            note += f" {ports_text(locks.locked).capitalize()} will be locked. "
        note += _CONNECT
    if locks.asked:
        note += (f" WARNING: {ports_text(locks.asked)} will be OPEN (unlocked): {OPEN_MEANS}. Go ahead only if the "
                 "user explicitly asked for an open port.")
    if locks.by_template:
        them = "them" if len(locks.by_template) > 1 else "it"
        note += (f" WARNING: {ports_text(locks.by_template)} will be OPEN (unlocked) because the template sets {them} "
                 f"open, not because the user asked: {OPEN_MEANS}. Tell the user and ask which they want. To lock "
                 f"{them}, call again with `template_ports` {lock_items(locks.by_template)} (a new preview); go ahead "
                 f"with {them} open only if the user says so.")
    return note


def result_note(locks: Locks) -> str:
    """Sentences for the answer of a confirmed create or edit: which ports are locked or open, and how to check."""
    note = ""
    if locks.locked:
        note += (f" {ports_text(locks.locked).capitalize()} {'are' if len(locks.locked) > 1 else 'is'} locked. "
                 + _CONNECT + " " + REACH_IT + " The pods tool shows each port's `access`: if one of these shows "
                 "`unlocked` once the pod runs, tell the user and lock it with set_port_lock.")
    if locks.asked:
        note += (f" {ports_text(locks.asked).capitalize()} {'are' if len(locks.asked) > 1 else 'is'} open: "
                 f"{OPEN_MEANS}.")
    if locks.by_template:
        note += (f" {ports_text(locks.by_template).capitalize()} {'are' if len(locks.by_template) > 1 else 'is'} "
                 f"open, as the template sets it: {OPEN_MEANS}. If the user didn't ask for that, tell them; "
                 "set_port_lock with locked=true locks a port without a restart.")
    return note
