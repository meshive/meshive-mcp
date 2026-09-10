"""도구 등록. 각 모듈은 `register(server)` 하나를 노출한다.

읽기 도구 13개(계약 §1, logs·transactions 포함) + 쓰기 도구 15개(계약 §2; meshive 0.1.x). 총 28개.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import (account, assets, billing, gpus, logs, machines, pods, servings, storages, tasks, templates,
               transactions, workspaces, write_pods, write_serverless, write_storages)

_MODULES = (account, workspaces, pods, storages, gpus, templates, servings, tasks, assets, machines, billing, logs,
            transactions, write_pods, write_storages, write_serverless)


def register_all(server: MCPServer) -> None:
    for module in _MODULES:
        module.register(server)
