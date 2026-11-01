"""Entry point.

  meshive-mcp                       # HTTP (default), MESHIVE_MCP_HOST/PORT
  meshive-mcp --transport stdio     # for development and debugging. Allows the MESHIVE_API_KEY fallback.
"""
from __future__ import annotations

import argparse
import logging

from .settings import settings


MAX_LOG_LINE = 8192


class BoundedLines(logging.Formatter):
    """Keep the two ends of a long record: the SDK's logs can repeat the caller's text, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if len(line) <= MAX_LOG_LINE:
            return line
        return f"{line[:MAX_LOG_LINE // 2]} ... {line[-MAX_LOG_LINE // 2:]}"


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

    # Named so that a missing parser fails here. The pure-Python one spends the event loop on a body in very small chunks.
    uvicorn.run(create_http_app(create_server()), host=args.host, port=args.port, log_level=args.log_level.lower(),
                http="httptools")


if __name__ == "__main__":
    main()
