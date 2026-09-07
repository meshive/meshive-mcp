"""도구 등록. 각 모듈은 `register(server)` 하나를 노출한다.

읽기 도구 11개(계약 §1). `logs` 는 백엔드 엔드포인트가 생기면 추가.
쓰기 도구는 meshive 0.1.x 이후.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import account, assets, billing, gpus, machines, pods, servings, storages, tasks, templates, workspaces

_MODULES = (account, workspaces, pods, storages, gpus, templates, servings, tasks, assets, machines, billing)


def register_all(server: MCPServer) -> None:
    for module in _MODULES:
        module.register(server)
