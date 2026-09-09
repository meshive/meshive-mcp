"""워크스페이스 인자 해석 규칙 (계약 0.1).

- 명시된 값이 16자리 hex 면 id 로 그대로 쓴다.
- 그 외 문자열은 **라벨**(workspace_name)로 보고 id 를 찾아준다 — 실측에서 모델이 라벨을 id 자리에
  넣는 실수가 잦았다. 못 찾으면 `unknown_workspace` 로 후보 목록과 함께 알린다.
- "all" 은 목록 도구에서 모든 워크스페이스를 뜻한다(CLI `pods --all` 과 동일).
- 생략: 1개면 자동 선택, 0개면 에러, 여러 개면 목록과 함께 "사용자에게 물어보라" 응답.
서버는 기본값을 저장하지 않는다(무상태). 모델이 대화 안에서 기억한다.
"""
from __future__ import annotations

import re
from typing import Any

from meshive import AsyncMeshive
from meshive.models import Workspace

from .errors import tool_error

NEEDS_WORKSPACE = "workspace"
ALL = "all"
_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _summary(workspaces: list[Workspace]) -> list[dict[str, str]]:
    return [{"workspace": w.namespace_name, "label": w.workspace_name} for w in workspaces]


def _by_label(workspaces: list[Workspace], label: str) -> str:
    wanted = label.strip().lower()
    hits = [w for w in workspaces if (w.workspace_name or "").lower() == wanted]
    if len(hits) == 1:
        return hits[0].namespace_name
    if not hits:
        raise tool_error("unknown_workspace", f"No workspace with id or label '{label}'.",
                         "Use one of the `workspace` ids listed here (not the label). "
                         "If unsure, call the workspaces tool and ask the user.",
                         workspaces=_summary(workspaces))
    raise tool_error("ambiguous_workspace", f"Several workspaces are labeled '{label}'.",
                     "Ask the user which one they mean and call again with its `workspace` id.",
                     workspaces=_summary(hits))


async def resolve(client: AsyncMeshive, workspace: str | None, *, required: bool = True) -> str | dict[str, Any] | None:
    """단일 워크스페이스 id 를 돌려준다. 여러 개 중 고를 수 없으면 needs_input dict.

    required=False 면 생략을 "지정 안 함"(None) 으로 통과시킨다(templates 처럼 생략에 의미가 있는 도구).
    """
    if workspace is not None and workspace.strip():
        workspace = workspace.strip()
        if _ID_RE.match(workspace):
            return workspace
        if workspace.lower() == ALL:
            raise tool_error("invalid_argument", "This tool does not accept workspace='all'.",
                             "Pass a single workspace id, or omit it.")
        return _by_label(await client.list_workspaces(), workspace)
    if not required:
        return None
    workspaces = await client.list_workspaces()
    if len(workspaces) == 1:
        return workspaces[0].namespace_name
    if not workspaces:
        raise tool_error("no_workspace", "The user has no workspace.",
                         "Ask the user to create a workspace in the console first.")
    return {
        "needs_input": NEEDS_WORKSPACE,
        "workspaces": _summary(workspaces),
        "next_step": "The user has several workspaces. Ask which one to use and call again with its "
                     "`workspace` id, or pass workspace='all' to search every workspace.",
    }


async def resolve_many(client: AsyncMeshive, workspace: str | None) -> list[str] | dict[str, Any]:
    """목록 도구용: "all" 이면 모든 id, 아니면 resolve 결과 하나를 리스트로."""
    if workspace is not None and workspace.strip().lower() == ALL:
        workspaces = await client.list_workspaces()
        if not workspaces:
            raise tool_error("no_workspace", "The user has no workspace.",
                             "Ask the user to create a workspace in the console first.")
        return [w.namespace_name for w in workspaces]
    one = await resolve(client, workspace)
    if isinstance(one, dict):
        return one
    return [one]  # type: ignore[list-item]


def needs_input(value: Any) -> bool:
    return isinstance(value, dict) and "needs_input" in value


_POD_NAME_RE = re.compile(r"^[0-9a-f]{16}-\d+$")


async def resolve_pod(client: AsyncMeshive, workspace: str, pod: str) -> str:
    """`pod` 가 pod_name(16 hex + "-N") 이면 그대로, 아니면 라벨(user_alias)로 보고 목록에서 찾는다.
    실측(2026-09-09): 모델이 방금 만든 파드를 라벨로 조회해 not_found 를 받고 목록을 다시 훑었다."""
    pod = (pod or "").strip()
    if _POD_NAME_RE.match(pod):
        return pod
    pods = await client.list_pods(workspace)
    hits = [p for p in pods if (p.user_alias or "").lower() == pod.lower()]
    if len(hits) == 1:
        return hits[0].pod_name
    candidates = [{"pod_name": p.pod_name, "name": p.user_alias, "status": p.status} for p in pods
                  if not getattr(p, "raw", {}).get("isDownloader")]
    if not hits:
        raise tool_error("not_found", f"No pod named '{pod}' in workspace {workspace}.",
                         "Use one of the `pod_name` values listed here, or call the pods tool.", pods=candidates)
    raise tool_error("ambiguous_pod", f"Several pods are named '{pod}'.",
                     "Ask the user which one and call again with its `pod_name`.", pods=candidates[:20])
