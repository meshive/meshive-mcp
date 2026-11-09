"""Entry point.

  meshive-mcp                       # HTTP (default), MESHIVE_MCP_HOST/PORT
  meshive-mcp --transport stdio     # for development and debugging. Allows the MESHIVE_API_KEY fallback.
"""
from __future__ import annotations

import argparse
import logging

import httptools
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

from .settings import settings


MAX_LOG_LINE = 8192
# The limit h11 sets by default.
MAX_HEAD_BYTES = 16 * 1024


class BoundedHeadProtocol(HttpToolsProtocol):
    """httptools, with a limit on a request's head: it has none of its own, and keeps every header field in memory.

    httptools rather than h11 because the pure-Python parser spends the event loop on a body sent in very small chunks.
    """

    head_bytes = 0

    def _count(self, size: int) -> None:
        self.head_bytes += size
        if self.head_bytes > MAX_HEAD_BYTES:
            raise httptools.HttpParserError("Request head too large")  # Answered with 400.

    def on_message_begin(self) -> None:
        self.head_bytes = 0
        super().on_message_begin()

    def on_url(self, url: bytes) -> None:
        self._count(len(url))
        super().on_url(url)

    def on_header(self, name: bytes, value: bytes) -> None:
        self._count(len(name) + len(value) + 4)
        super().on_header(name, value)


class BoundedLines(logging.Formatter):
    """Keep the two ends of a long record: the SDK's logs can repeat the caller's text, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if len(line) <= MAX_LOG_LINE:
            return line
        return f"{line[:MAX_LOG_LINE // 2]} ... {line[-MAX_LOG_LINE // 2:]}"


def raise_open_file_limit() -> None:
    """Raise the soft limit on open files to the hard one.

    At the usual soft limit of 1024, a server holding that many connections stops accepting new ones, health checks
    included, long before memory runs out. The hard limit stays the operator's to set.
    """
    try:
        import resource
    except ImportError:  # Windows
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY:
        return
    # An unlimited hard limit (macOS) can't be the soft one there: the most it takes is OPEN_MAX.
    target = 10240 if hard == resource.RLIM_INFINITY else hard
    if soft >= target:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError):
        logging.getLogger(__name__).warning("Couldn't raise the open file limit from %d.", soft)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(BoundedLines("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler])
    # At INFO the SDK logs every refused tool call, and those need no record.
    if level == "INFO":
        logging.getLogger("mcp.server.mcpserver.server").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="meshive-mcp", description="Meshive MCP server")
    parser.add_argument("--transport", choices=("http", "stdio"), default="http")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    configure_logging(args.log_level.upper())

    from .server import create_http_app, create_server

    if args.transport == "stdio":
        settings.env_api_key_fallback = True
        create_server().run(transport="stdio")
        return

    import uvicorn

    raise_open_file_limit()
    uvicorn.run(create_http_app(create_server()), host=args.host, port=args.port, log_level=args.log_level.lower(),
                http=BoundedHeadProtocol)


if __name__ == "__main__":
    main()
