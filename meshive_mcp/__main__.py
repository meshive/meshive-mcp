"""엔트리포인트.

  meshive-mcp                       # HTTP (기본), MESHIVE_MCP_HOST/PORT
  meshive-mcp --transport stdio     # 개발·디버깅용. MESHIVE_API_KEY 폴백 허용.
"""
from __future__ import annotations

import argparse
import logging

from .settings import settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="meshive-mcp", description="Meshive MCP server")
    parser.add_argument("--transport", choices=("http", "stdio"), default="http")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .server import create_http_app, create_server

    if args.transport == "stdio":
        settings.env_api_key_fallback = True
        create_server().run(transport="stdio")
        return

    import uvicorn

    uvicorn.run(create_http_app(create_server()), host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
