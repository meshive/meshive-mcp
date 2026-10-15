"""Tool registration. Each module exposes a single `register(server)`.

Read tools (including logs, transactions and download_links) + write tools. tests/test_tools.py pins the count.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import (account, assets, billing, cpus, gpus, logs, machines, pods, servings, storages, tasks, templates,
               transactions, workspaces, write_pods, write_serverless, write_storages)

_MODULES = (account, workspaces, pods, storages, gpus, cpus, templates, servings, tasks, assets, machines, billing, logs,
            transactions, write_pods, write_storages, write_serverless)


def register_all(server: MCPServer) -> None:
    for module in _MODULES:
        module.register(server)
