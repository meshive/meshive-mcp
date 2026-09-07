"""워크스페이스 인자 해석 규칙 (계약 0.1).

- 명시되면 그대로.
- 생략: 1개면 자동 선택, 0개면 에러, 여러 개면 목록과 함께 "사용자에게 물어보라" 응답.
서버는 기본값을 저장하지 않는다(무상태). 모델이 대화 안에서 기억한다.
"""
from __future__ import annotations

from typing import Any

from meshive import AsyncMeshive

from .errors import tool_error

NEEDS_WORKSPACE = "workspace"


async def resolve(client: AsyncMeshive, workspace: str | None) -> str | dict[str, Any]:
    if workspace:
        return workspace
    workspaces = await client.list_workspaces()
    if len(workspaces) == 1:
        return workspaces[0].namespace_name
    if not workspaces:
        raise tool_error("no_workspace", "The user has no workspace.",
                         "Ask the user to create a workspace in the console first.")
    return {
        "needs_input": NEEDS_WORKSPACE,
        "workspaces": [{"workspace": w.namespace_name, "label": w.workspace_name} for w in workspaces],
        "next_step": "The user has several workspaces. Ask which one to use, then call again with `workspace`.",
    }


def needs_input(value: Any) -> bool:
    return isinstance(value, dict) and "needs_input" in value
