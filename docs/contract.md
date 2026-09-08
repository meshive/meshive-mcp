# meshive-mcp 도구 목록 · 스키마 초안 (v0, 2026-09-07)

이 문서는 세 표면의 **단일 계약**이다. 필드명·의미가 세 곳에서 같아야 한다.

| 표면 | 형태 |
|---|---|
| WebServerBackend `/v1/sdk/*` | 엔드포인트 + 요청/응답 모델 |
| meshive-python 0.1.0 | `Meshive.create_pod(...)` 등 메서드 시그니처 |
| meshive-mcp | MCP 도구 `inputSchema` / 응답 JSON |

전제(확정): 원격 streamable HTTP 단일, 무상태(`GET /mcp` 405), `Authorization: Bearer meshive_...` 를 WSB 로 그대로 전달, OAuth 없음, 스코프 `read` / `write`.

---

## 0. 공통 규약

### 0.1 워크스페이스 해석
- 모든 워크스페이스 단위 도구의 `workspace` 는 **선택**.
- 값이 16자리 hex 면 id. 그 외 문자열은 **라벨**(workspace_name)로 보고 서버가 id 로 바꿔 준다(2026-09-07 실측: 모델이 라벨을
  id 자리에 넣는 실수가 잦았다). 못 찾으면 `unknown_workspace` + 후보 목록, 라벨이 겹치면 `ambiguous_workspace`.
- 목록 도구(pods, storages, servings)는 `"all"` 을 받아 모든 워크스페이스를 합쳐 준다(응답 `workspace: "all"`). tasks/assets 는
  서버 페이징이 워크스페이스 단위라 `"all"` 을 거절한다.
- 백엔드 403 "not a member of workspace" 는 `unknown_workspace` 로 번역한다(모델 입장에서는 잘못된 id).
- 생략 시: 사용자 워크스페이스가 1개 → 그것을 사용. 여러 개 → `isError` 없이 아래를 반환하고 종료.
  ```json
  {"needs_input": "workspace", "workspaces": [{"name": "ws-a1b2", "label": "research"}, ...],
   "next_step": "Ask the user which workspace to use, then call again with `workspace`."}
  ```
- 서버는 기본값을 저장하지 않는다(무상태). 모델이 대화 안에서 기억한다.

### 0.2 목록 캡핑
- 모든 목록 도구: `limit` 기본 20, 최대 100. `cursor` 는 불투명 문자열(base64 offset). 응답에 `next_cursor`(없으면 null), `total`(알 수 있을 때만).
- 백엔드에서 전체 목록을 주는 pods/tasks/machines/templates/servings 는 v1 에서 MCP 서버가 잘라 내고, 백엔드 `limit/cursor` 는 후속 작업으로 붙인다(커서 형식은 처음부터 동일하게).

### 0.3 응답 형식
- `content[0].text` 에 compact JSON(공백·줄바꿈 없음). 같은 객체를 `structuredContent` 로도 싣는다.
- 백엔드 Decimal 문자열은 정규화한다: `"0E-8"` → `"0"`, `"0.87741500"` → `"0.877415"`. 정수 문자열(id 가능)은 손대지 않는다.
- 원시 응답(`raw`)은 싣지 않는다. SDK 모델의 정규화 필드만.
- 크기·비율 단위는 필드명에 포함(`vram_gb`, `usage_rate`, `price_per_hour_usd`).

### 0.4 에러 → 모델용 지시문
`isError: true` 이고 본문은 항상 `{code, message, next_step}`.

| HTTP | code | next_step (모델에게) |
|---|---|---|
| 401 | `no_api_key` / `invalid_api_key` | "Ask the user to create a Meshive API key in the console (workspace Settings → Secret) and put it in this MCP server's Authorization header as `Bearer meshive_...`. Do not retry until they confirm." |
| 403 (scope) | `write_scope_required` | "This action needs a key with the `write` scope. Ask the user to issue one in the console." |
| 403 (role) | `not_workspace_admin` | "The user is not an admin of this workspace. Do not retry." |
| 402 / 크레딧 부족 | `insufficient_credit` | "Ask the user to top up credit at console.meshive.ai before retrying." |
| 404 | `not_found` | "Verify the id with the corresponding list tool; do not guess ids." |
| 409 | `conflict` | 본문의 안내대로(예: 같은 이름의 파드가 이미 있음 → `pods` 도구로 확인). |
| 429 | `rate_limited` | "Do not retry immediately. Wait at least {retry_after}s." |
| 5xx / 503 | `temporarily_unavailable` | "Wait {retry_after}s and retry once. If it fails again, tell the user." |
| 도구 인자 오류 | `invalid_argument` | 어떤 필드가 왜 틀렸는지 + 허용값. |

### 0.5 돈이 드는 도구의 확인 규약
- 생성·배포·제출 도구는 `confirm: boolean`(기본 false)을 받는다.
- `confirm=false` 면 **아무것도 만들지 않고** 견적을 반환한다(= dry-run). 응답에 `"next_step": "Show this estimate to the user. Call again with confirm=true only after they agree."`
- `confirm=true` 라도 `max_price_per_hour_usd` 가 있으면 서버 계산가가 그보다 크면 거절(`price_exceeds_cap`). 모델이 사용자에게 보여준 가격을 서버가 보증하는 장치.
- 삭제 도구도 `confirm` 필수. false 면 삭제 대상 요약(연결 스토리지, 실행 중 여부)만 반환.

### 0.6 멱등성
- 백엔드 POST 는 `Idempotency-Key` 헤더를 받아 24h 동안 같은 응답을 재생(Redis). SDK 가 호출마다 UUID 를 만들고 **자체 재시도(5xx/timeout)에서만 재사용**.
- 모델 수준 재시도는 이름 유일성으로 막는다: 같은 워크스페이스에 같은 `name` 의 활성 파드/스토리지가 있으면 409.

### 0.7 클라이언트 식별
- MCP `initialize` 의 `clientInfo.name/version` 을 WSB 요청의 `X-Meshive-Client: <name>/<version>` 헤더로 전달. `User-Agent` 는 `meshive-mcp/<ver> meshive-python/<ver>`.
- WSB 는 API 키 인증 로그에 두 헤더를 남긴다(측정 목적, 인가에는 쓰지 않음).

### 0.8 어노테이션
| 종류 | readOnlyHint | destructiveHint | idempotentHint |
|---|---|---|---|
| 조회 | true | false | true |
| estimate | true | false | true |
| create/deploy/submit | false | false | false |
| stop/start/restart/scale/pause | false | false | true |
| delete | false | **true** | true |

---

## 1. 읽기 도구 (11개, `read` 스코프, `gpus` 는 무인증 가능)

list/get 은 id 인자 유무로 한 도구.

| 도구 | 입력 | 출력(핵심) | 백엔드 |
|---|---|---|---|
| `account` | — | email, name, credit{balance, paid, bonus, auto_recharge}, api_key{prefix, scopes, expires_at} | `/me`, `/credit`, `/api-keys`(현재 키만) |
| `workspaces` | `workspace?` | 목록: name,label,role,pod_count. 단건: 상세 + 비용 요약 + 멤버 | `/workspaces`, `/workspaces/{ns}`, `/members` |
| `pods` | `workspace?`, `pod?`, `status?`, `include_metrics?=false`, `limit?`, `cursor?` | pod_name,name,status,rental_type,gpu,price_per_hour_usd,endpoints,created_at (+metrics) | `/pods`, `/pods/{p}`, `/pods/{p}/metrics` |
| `storages` | `workspace?`, `storage?`, `limit?`, `cursor?` | pv_name,name,type,size_gb,used_rate,linked_pods,price_per_hour_usd | `/storages`, `/storages/{s}` |
| `gpus` | `rental_type?=demand`, `min_vram_gb?` | gpu_model,vram_gb,price_per_hour_usd,available_gpus,max_gpus_per_pod,vcpu_recommended,ram_recommended_gb | 키 있음: `/gpus`(실시간 재고). 키 없음: `/landing/pricing-catalog`(가격표만, `availability: "unknown"`) |
| `templates` | `workspace?`, `template?`, `app_type?`, `hardware_type?`, `limit?`, `cursor?` | template_id,name,description,image,deploy_type,app_type,hardware_type,cuda,framework. 단건: envs(시크릿 값 마스킹), ports | `/templates`, `/templates/{id}` |
| `servings` | `workspace?`, `serving?`, `limit?`, `cursor?` | serving_id,model,framework,status,paused,replicas{min,max,current,healthy},endpoint_url,price_per_hour_usd | `/servings`, `/servings/{id}` |
| `tasks` | `workspace?`, `task?`, `status?`, `limit?`, `cursor?` | task_id,name,status,image,gpu,started_at,finished_at,exit_code,cost_usd | `/tasks`, `/tasks/{id}` |
| `assets` | `workspace?`, `asset?`, `asset_type?`, `status?`, `limit?`, `cursor?` | asset_id,name,type,status,size_gb,versions. 단건: 버전·파일 목록 | `/assets`, `/assets/{id}`, `/assets/storage-summary` |
| `machines` | `machine?`, `include_metrics?=false` | (호스트) machine_id,name,type,status,gpu,earning_hourly_usd,uptime_rate | `/machines`, `/machines/{id}`, `/metrics` |
| `billing_history` | `kind: "credit"｜"earnings"`, `since?`, `until?`, `limit?`, `cursor?` | 항목 목록 + 합계 | `/credit/history`, `/earnings` |

**신규 read 엔드포인트(PR-B5 로 구현됨, 2026-09-08):**
- `logs` 도구: `workspace?`, `pod?`｜`task?`, `tail?=200`(≤1000), `wait?=8`(≤15s), `container?` → `{pod_name, source: live|archive|external|none, lines[{line, ts?}], count, truncated, note}`,
  본문 64KB 상한(앞에서 잘라 `truncated`). 백엔드 `GET /v1/sdk/pods/{p}/logs`(Watcher 링버퍼, 비어 있으면 구독으로 Watcher 를 깨워 `wait` 초 대기,
  종료 task 파드는 R2 아카이브), `GET /v1/sdk/tasks/{id}/logs`(내부 태스크는 파드 경로, 외부 provider 는 `cursor` 증분; 응답에 `task_id, finished, next_cursor`).
  `since` 는 제외(링버퍼가 최근 1000줄만 보관). → 읽기 도구 12개.

---

## 2. 쓰기 도구 (`write` 스코프)

### 2.1 파드

#### `estimate_pod` (readOnly)
`create_pod` 와 같은 입력, `confirm` 없음. 출력:
```json
{"price_per_hour_usd": "0.62", "breakdown": {"gpu": "0.55", "cpu_ram": "0.05", "storage": "0.02"},
 "availability": {"available_gpus": 7, "max_gpus_per_pod": 4},
 "estimated_credit_hours": 41.9,
 "next_step": "Show the price to the user. Then call create_pod with the same arguments and confirm=true."}
```
백엔드: `POST /v1/sdk/pods/estimate` (read 스코프 허용). 내부에서 템플릿 해석 → 노드 선택 dry-run → K8sCS 가격 계산 재사용.

#### `create_pod`
| 필드 | 타입 | 필수 | 비고 |
|---|---|---|---|
| `workspace` | string | 0.1 규약 | |
| `name` | string | ✓ | 사용자 라벨(`user_alias`). 워크스페이스 내 활성 파드와 중복이면 409 |
| `template_id` | int | ✓ | `templates` 도구의 id. 서버가 템플릿 전체를 해석(FE 처럼 객체를 보내지 않음) |
| `gpu_model` | string | GPU 템플릿이면 ✓ | `gpus` 도구의 `gpu_model` 그대로 |
| `gpu_count` | int | | 기본 1 |
| `gpu_vram_gb` | int | | 같은 모델의 VRAM 티어 구분(RTX 5060 Ti 8/16 등). 생략 = 서버 기본 |
| `rental_type` | `"demand"｜"spot"` | | 기본 demand. spot 은 hostPath 볼륨 불가(서버 검증) |
| `vcpu` / `ram_gb` | int | | 생략 시 GPU 권장값(`vcpu_recommended`, `ram_recommended_gb`) |
| `disk_gb` | int | | 컨테이너 임시 디스크. 생략 시 템플릿 기본 |
| `volumes` | list | | `[{"storage": "<pv_name>", "mount_path": "/data"}]` 기존 스토리지 연결. 신규 생성은 `create_storage` 로 분리 |
| `env` | object | | 템플릿 env 위에 덮어씀. 시크릿 지정은 `secret_keys` |
| `secret_keys` | list[string] | | |
| `ports` | list | | `[{"port": 8888, "name": "jupyter", "external": true}]` 템플릿 endpoint 에 추가. 최대 `resource.max_endpoints_per_pod` |
| `command` | string | | overwrite_command |
| `internet_premium` / `uptime_premium` / `cpu_premium` | bool | | 기본 false |
| `region` | string | | `target_location`(예: "KR"). 생략 = 아무 곳 |
| `max_price_per_hour_usd` | string | | 0.5 규약 |
| `confirm` | bool | ✓ | false → estimate 반환 |
| `wait` | `"none"｜"running"` | | 기본 none. running 이면 최대 `wait_timeout_s`(기본 300) 동안 대기 후 반환 |

응답: `pods` 단건 스키마 + `price_per_hour_usd` + `"next_step"`(예: "Pod is provisioning. Poll with the pods tool; endpoints appear when status is running.").

백엔드: `POST /v1/sdk/pods` → 내부에서 `StatefulSetCreateRequest` 를 조립(template 해석, `resource_price/storage_price` 서버 계산, `node_name/cluster_name` 서버 결정). `verify_admin_role` + `verify_credit_balance(require_paid_balance=True)` 그대로. `rate_limit_k8s_create` 재사용.
SDK: `create_pod(name, template_id, *, workspace=None, gpu_model=None, gpu_count=1, ..., wait=None) -> Pod`, `estimate_pod(...) -> PodEstimate`.

#### `stop_pod` / `start_pod` / `restart_pod`
- 입력: `workspace?`, `pod`(pod_name). `start_pod` 는 `placement?: "same_node"｜"any_node"` 추가(기본 same_node).
- 출력: 갱신된 pod 단건 + `next_step`.
- 백엔드: `POST /v1/sdk/pods/{p}/stop|start|restart` (기존 statefulset stop/start/restart 로 위임).
- SDK: `stop_pod(pod, workspace=None)`, `start_pod(pod, workspace=None, placement="same_node")`, `restart_pod(...)`.

#### `delete_pod` (destructive)
- 입력: `workspace?`, `pod`, `delete_local_storages?: list[string]`(hostPath 볼륨 이름을 명시한 것만 같이 삭제 — 웹 확인 다이얼로그와 동일 의미), `confirm`.
- `confirm=false`: `{pod, status, attached_storages: [{name, type, size_gb, will_be_deleted: bool}], next_step: "Ask the user to confirm deletion..."}`
- 백엔드: `DELETE /v1/sdk/pods/{p}?deleteLocalStoragePvNames=`.

### 2.2 스토리지
#### `create_storage`
`workspace?`, `name`, `size_gb`, `storage_type: "nfs"｜"hostPath"`(기본 nfs), `encrypted?`(hostPath 만), `region?`, `max_price_per_hour_usd?`, `confirm`.
→ 백엔드 `POST /v1/sdk/storages`. **확인 필요**: 웹은 스토리지를 파드 생성 볼륨 파라미터로 만드는지, 별도 PVC 생성인지(`routers/kubernetes/pvc.py` 는 `storage_class/access_modes` 를 받음). 이 도구의 최종 형태는 그 확인 뒤 확정.
#### `delete_storage` (destructive)
`workspace?`, `storage`, `confirm`. 연결된 파드가 있으면 409 + 파드 목록.

### 2.3 서버리스 serving
#### `deploy_serving`
| 필드 | 필수 | 비고 |
|---|---|---|
| `workspace?` | | |
| `model_registration_id` | ✓ | 등록된 모델 id. 모델 등록 자체(`POST /serving/model`, HF 토큰 등)는 v1 도구 범위 밖 → `next_step` 으로 콘솔 안내 |
| `min_replicas` / `max_replicas` | | 기본 1 / 3 |
| `autoscale` | | 기본 true |
| `price_cap_per_hour_usd` | ✓ | replica 당 상한. 웹은 항상 보냄 |
| `max_context_tokens` | | `max_model_len` |
| `confirm` | ✓ | false → 예상 replica 단가·상한 요약 |
→ 백엔드 `POST /v1/sdk/servings` (기존 `ServingDeployRequest` 로 위임). 응답: servings 단건.

#### `scale_serving` — `serving`, `min_replicas?`, `max_replicas?`, `autoscale?` → `PATCH /v1/sdk/servings/{id}/scale`
#### `pause_serving` — `serving`, `paused: bool` → `PATCH /v1/sdk/servings/{id}/pause`
#### `delete_serving` (destructive) — `serving`, `confirm` → `DELETE /v1/sdk/servings/{id}`

### 2.4 서버리스 task
#### `submit_task`
| 필드 | 필수 | 비고 |
|---|---|---|
| `workspace?` | | |
| `name` | ✓ | |
| `script` | ✓ | 파이썬 스크립트 본문(`script_content`). 크기 상한 256KB |
| `requirements` | | requirements.txt 본문 |
| `template_id` 또는 `image` | 둘 중 하나 | |
| `env` / `secret_keys` / `args` | | |
| `gpu_model` / `gpu_count` / `gpu_vram_gb` | GPU 면 ✓ | |
| `cpu_preset` | CPU 면 ✓ | 허용값은 `invalid_argument` 로 안내 |
| `max_duration_s` | ✓ | |
| `webhook_url` | | |
| `input_assets` | | `[{"asset": "<asset_id>", "version"?: n}]` 최대 20 |
| `max_price_per_hour_usd` | | |
| `confirm` | ✓ | false → 예상 시간당 단가 × max_duration 상한 비용 |
→ `POST /v1/sdk/tasks` (기존 `TaskSubmitRequest` 로 위임). 응답: tasks 단건 + `next_step: "Poll with tasks; fetch output with logs."`
#### `stop_task` — `task` → `POST /v1/sdk/tasks/{id}/stop`

---

## 3. 백엔드 변경 요약 (WebServerBackend, dev)

1. `MeshiveApiKeyScope.WRITE` + 발급 API `scopes` 파라미터 + 콘솔 UI 선택 + **write 키 만료 필수**(정책 값 결정 필요, 예: 최대 90일).
2. `/v1/sdk` write 라우터: 위 표의 15개 엔드포인트. 각각 `require_api_key_scopes(WRITE)` + 기존 rate limiter(`rate_limit_k8s_create/update/delete`) + 기존 서비스 위임. 로직 중복 없음, 요청 모델만 새로 정의.
3. `POST /v1/sdk/pods/estimate` (read 스코프).
4. `GET /v1/sdk/pods/{p}/logs`, `GET /v1/sdk/tasks/{id}/logs`.
5. `Idempotency-Key` 미들웨어(POST 한정, Redis 24h).
6. `X-Meshive-Client` 로깅.
7. 목록 엔드포인트 `limit/cursor`(후속 가능).

## 4. meshive-python 0.1.0 요약
- 동기·비동기 각각: `estimate_pod, create_pod, stop_pod, start_pod, restart_pod, delete_pod, create_storage, delete_storage, deploy_serving, scale_serving, pause_serving, delete_serving, submit_task, stop_task, get_pod_logs, get_task_logs` (16개).
- 모델: `PodEstimate`, `PodCreateSpec`(선택), `Logs`.
- 재시도: POST 는 `Idempotency-Key` 를 붙였을 때만 5xx/timeout 재시도.
- CLI: `pod create/stop/start/restart/delete`, `storage create/delete`, `serving deploy/scale/pause/delete`, `task submit/stop`, `logs`. 돈이 들거나 파괴적인 커맨드는 `--yes` 없으면 견적/요약을 보여주고 확인 프롬프트.

## 5. 도구 수
- 읽기 12 (account, workspaces, pods, storages, gpus, templates, servings, tasks, assets, machines, billing_history, logs)
- 쓰기 15 (estimate_pod, create_pod, stop_pod, start_pod, restart_pod, delete_pod, create_storage, delete_storage, deploy_serving, scale_serving, pause_serving, delete_serving, submit_task, stop_task) — 14 + estimate
- 합계 27. 각 도구 description 은 영어, "언제 쓰는지 / 먼저 무엇을 호출해야 하는지 / 결과를 사용자에게 어떻게 보여줄지" 세 문장 규칙.

## 6. 미확정
- 스토리지 생성 플로우(2.2).
- write 키 만료 상한 값.
- 로그 엔드포인트의 소스(K8sCS RPC 로 pod log tail 인지, 기존 `routers/kubernetes/logs.py` 재사용인지).
- `update_pod`(리소스 변경)와 `reallocate` 는 v1 제외. 필요 시 0.1.x 에서.
