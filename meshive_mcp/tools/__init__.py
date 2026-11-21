"""Tool registration. Each module exposes a single `register(server)`.

Read tools (including logs, transactions and download_links) + write tools; `hosting` has the host tools besides
`machines`, `account` also has `activity`, the log of what the account's API keys did, and `write_endpoints` has the
port locks and access keys of a pod's public ports, and `feedback` sends a report to the Meshive team.
tests/test_tools.py pins the count.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import (account, assets, billing, buckets, commands, cpus, feedback, gpus, hosting, logs, machines, pods,
               servings, storages, tasks, templates, transactions, workspaces, write_endpoints, write_pods,
               write_serverless, write_storages)
from . import pod_assets
from . import write_templates

_MODULES = (account, workspaces, pods, storages, gpus, cpus, templates, servings, tasks, assets, buckets, machines,
            hosting, billing, logs, transactions, write_pods, write_endpoints, commands, write_storages,
            write_serverless, feedback)
# Assets in an existing pod (add_pod_asset, remove_pod_asset).
_MODULES += (pod_assets,)
# Custom pod templates (save_template, delete_template).
_MODULES += (write_templates,)


def register_all(server: MCPServer) -> None:
    for module in _MODULES:
        module.register(server)
