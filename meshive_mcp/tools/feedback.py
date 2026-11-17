"""send_feedback: a bug report, idea or complaint to the Meshive team, as the console's Send feedback does.

The agent offers it (once per conversation) when a Meshive tool fails unexpectedly or the user says something about
Meshive is broken, confusing or missing. The preview is the exact text that would be sent; nothing goes out until the
user agrees in the conversation.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from meshive import AsyncMeshive
from meshive.exceptions import NotFoundError, RateLimitError

from ..client import meshive_client
from ..errors import invalid_argument, tool_error
from ._common import CREATE, call, meshive_tool, preview

KINDS = ("bug", "idea", "complaint")
MAX_SUMMARY = 200
# The summary, a blank line and the details, as the server stores them.
MAX_TEXT = 4000
# The server keeps `tool` and `error_code` only in this shape, so no names or sentences ride along in them.
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

REVIEW_NOTE = "The Meshive team reviews every report."
NEXT_STEP_SENT = (f"Tell the user it was sent. {REVIEW_NOTE} Don't promise a reply, a fix, a reward or credit, and don't "
                  "offer to send feedback again in this conversation unless the user asks.")
NEXT_STEP_UNAVAILABLE = ("Nothing was sent. Tell the user that Meshive isn't taking reports from agents right now. "
                         "Don't retry.")
NEXT_STEP_TOO_MANY = ("Nothing was sent: this account sent several reports in a short time. Tell the user; don't "
                      "retry on your own.")


def _clean(text: str | None) -> str:
    """The text as the server stores it: control characters other than line breaks and tabs removed, the ends trimmed."""
    return "".join(ch for ch in (text or "") if ch in "\n\t" or unicodedata.category(ch) != "Cc").strip()


def _identifier(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if _IDENTIFIER.fullmatch(value) else None


def _refusal(error: ToolError) -> ToolError:
    """The server's own refusals of a report, said for this tool: its 404 when it isn't taking feedback (or a server
    without the route), and its 429 for too many reports. Every other error, the general API rate limit included,
    stays as it is."""
    cause = error.__cause__
    if isinstance(cause, NotFoundError) and (cause.title == "Feedback Unavailable" or cause.title is None):
        return tool_error("feedback_unavailable", "Meshive isn't taking feedback through the API right now.",
                          NEXT_STEP_UNAVAILABLE)
    if isinstance(cause, RateLimitError) and cause.title == "Too Many Reports":
        return tool_error("feedback_rate_limited", "Several reports were sent from this account in a short time.",
                          NEXT_STEP_TOO_MANY, retry_after=int(cause.retry_after or 0) or None)
    return error


def register(server: MCPServer) -> None:
    @meshive_tool(server, "send_feedback", title="Send Feedback to Meshive", annotations=CREATE)
    async def send_feedback(ctx: Context[Any, Any], summary: str, kind: str | None = None, details: str | None = None,
                            tool: str | None = None, error_code: str | None = None, confirm: bool = False,
                            operation_id: str | None = None) -> dict[str, Any]:
        """Send a bug report, an idea or a complaint to the Meshive team, as the console's Send feedback does.
        When to offer it: a Meshive tool failed unexpectedly (code server_error, or an error that isn't a JSON object
        with code, message and next_step), or the user says something about Meshive is broken, confusing or missing.
        Then ask the user once whether to send it to Meshive, at most once per conversation, and only send it if they
        say yes. Don't offer it for errors whose next_step already explains them (rate limits, credit, a wrong id, a
        refused request), and never send one the user didn't agree to.
        `summary` is one line under 200 characters. `details` (optional) says, in your own words, what the user did,
        what they expected and what happened; summary and details together stay under 4000 characters. `kind` is bug,
        idea or complaint. For a failed call, `tool` is that tool's name and `error_code` its `code` (plain lowercase
        identifiers; anything else is left out).
        Never put passwords, API or access keys, tokens, environment values, log or command output, file contents, or
        workspace, pod, storage and other resource names or ids in the text; describe the problem instead.
        With confirm=false (default) it returns exactly what would be sent and sends nothing: show it to the user, and
        call again with confirm=true only after they agree. Don't promise a reward, credit or a reply: say only that
        the team reviews every report."""
        head = " ".join(_clean(summary).split())
        body = _clean(details)
        if not head:
            raise invalid_argument("`summary` is required: one line that says what the report is about.")
        if len(head) > MAX_SUMMARY:
            raise invalid_argument(f"`summary` must be one line under {MAX_SUMMARY} characters; put the rest in "
                                   "`details`.")
        if body and len(head) + 2 + len(body) > MAX_TEXT:
            raise invalid_argument(f"`summary` and `details` together must stay under {MAX_TEXT} characters.")
        if kind is not None:
            kind = kind.strip().lower()
            if kind not in KINDS:
                raise invalid_argument(f"`kind` must be one of {', '.join(KINDS)}.", allowed=list(KINDS))
        tool_name, code = _identifier(tool), _identifier(error_code)
        report = {"kind": kind, "summary": head, "details": body or None, "tool": tool_name, "error_code": code}
        if not hasattr(AsyncMeshive, "send_feedback"):
            # Installed next to a meshive SDK release that predates send_feedback: say so before the user agrees.
            raise tool_error("feedback_unavailable", "This MCP server can't send feedback yet (its meshive SDK "
                             "predates it).", NEXT_STEP_UNAVAILABLE)
        if not confirm:
            dropped = [name for name, given, kept in (("tool", tool, tool_name), ("error_code", error_code, code))
                       if (given or "").strip() and kept is None]
            question = ("Send this report to the Meshive team? It goes from the user's account, with the account's "
                        f"email. {REVIEW_NOTE}")
            out = preview("send_feedback", {"to": "the Meshive team", "report": report}, question)
            if dropped:
                out["left_out"] = {"fields": dropped, "why": "Only plain lowercase identifiers are kept."}
            return out
        async with meshive_client(ctx) as client:
            try:
                await call(client.send_feedback, head, kind=kind, details=body or None, tool=tool_name,
                           error_code=code)
            except ToolError as error:
                raise _refusal(error) from None
        return {"sent": True, "report": report, "next_step": NEXT_STEP_SENT}
