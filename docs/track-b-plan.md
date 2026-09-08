# 트랙 B 계획 — 쓰기 표면 (WSB → SDK 0.1.0 → MCP 쓰기 도구) · v1, 2026-09-07

계약은 [contract.md](contract.md). 이 문서는 **구현 순서·범위·결정 사항·위험**만 다룬다. 코드 착수 전 문서.

---

## 0. 코드로 확인한 사실 (계획의 전제)

| # | 사실 | 계획에 미치는 영향 |
|---|---|---|
| F1 | **가격은 K8sCS 가 노드 기준으로 재계산**한다(`calculate_gpu_pod_price(node_name, …)`). 클라이언트가 보낸 `resource_price` 는 불일치 시 **`logger.error` 만 남기고 거절하지 않는다**(K8sCS statefulset.py:699). 청구는 K8sCS 계산값. | 에이전트용 create 는 WSB 가 웹과 같은 공식(GpuDistribution 의 `gpu_price + cpu_price_per_core×vcpu + ram_price_per_gb×ram` + 프리미엄)으로 **견적**을 내서 `resource_price` 에 실으면 된다. 견적은 "추정"이고 실제 청구가는 착지 노드에 따라 소폭 다를 수 있다 → 응답에 명시. `max_price_per_hour_usd` 캡은 1차로 WSB 견적 기준, 2차(선택)로 K8sCS 에 tx params 로 넘겨 실제가에 강제. |
| F2 | 기존 쓰기 핸들러가 전부 있다: statefulset create/stop/start/restart/delete, pv create/patch/delete, serving deploy/scale/pause/delete, tasks submit/stop, tasks `logs/tail`(cursor/limit, Redis). | `/v1/sdk` 쓰기 라우터는 **요청 모델만 새로 만들고 핸들러/서비스에 위임**. 읽기 라우터와 같은 자세, 로직 중복 없음. |
| F3 | 파드 로그는 SSE 스트림(`/v1/kubernetes/logs/stream`, 쿠키 토큰) 뿐. prefill 로 최근 N줄을 K8sCS 에서 받아 온다. | 단발 tail 엔드포인트 신설 필요. 스트림의 prefill 소스를 그대로 재사용. |
| F4 | 스토리지 생성 = `POST /v1/kubernetes/pv` (`user_alias`, `namespace_name`, `storage_type` nfs\|hostPath, `capacity`(GB), `disk_type` HDD\|SSD\|NVMe, `encryption`, `target_location`). 파드 볼륨은 기존 PV 를 `pv_name` 으로 연결. | 계약 §2.2 `create_storage` 확정 가능. 미결 해소. |
| F5 | 파드 create 는 `verify_admin_role` + `verify_credit_balance(require_paid_balance=True)`. 스팟은 hostPath 볼륨 불가. 엔드포인트 수 상한 `resource.max_endpoints_per_pod`. `node_name/cluster_name/reservation_id/is_downloader` 는 공개 라우터가 서버에서 초기화. | 키 소유자가 워크스페이스 **admin** 이어야 쓰기 가능(403 → `not_workspace_admin`). 같은 가드를 그대로 적용. |
| F6 | rate limit 티어: K8S_RESOURCE_CREATE 5 / UPDATE 10 / DELETE 10 / STANDARD 100 (윈도우당, Redis). | 쓰기 엔드포인트에 기존 의존성(`rate_limit_k8s_*`)을 그대로 붙인다. 신규 티어 불필요. |
| F7 | API 키 발급은 JWT 전용, 활성 키 최대 10, `expires_in_days` 선택(≤365), scope 는 `read` 고정. `MeshiveApiKeyScope` enum + CSV validator. | WRITE 추가는 enum·발급 파라미터·FE 선택 UI. 검증 의존성은 이미 scope 목록 비교. |
| F8 | Redis 는 rate limiter 가 쓰고 있다(`rate_limit.py`). | 멱등성 키 저장소로 재사용. |
| F9 | dev 클러스터 GPU 재고: RTX 5090 1대, 온디맨드 가용 0, 스팟 1. | 쓰기 e2e 는 **CPU 파드** 또는 스팟 1장으로만 가능. 테스트가 재고를 점유하므로 짧게 만들고 바로 지운다. |
| F10 | 실측에서 모델은 라벨을 id 자리에 넣고, needs_input 을 받으면 스스로 순회한다. | 쓰기 도구도 라벨 해석·`confirm=false` 견적 응답을 **서버가** 강제해야 한다(모델의 주의력에 기대지 않기). |

---

## 1. 착수 전 결정 (사용자)

| # | 결정 | 권장 |
|---|---|---|
| D1 | **write 키 만료** | 필수, 최대 90일, 기본 30일. 읽기 키는 현행(무기한 허용) 유지. 유출 시 파드 생성=크레딧 소진이라 읽기 키와 같은 정책을 쓸 수 없다. |
| D2 | **스코프 조합** | `read`, `write` 두 값. write 는 read 를 포함하지 않고 **CSV "read,write"** 로 발급(검증이 목록 비교라 자연스럽다). 콘솔 UI 는 "Read only / Read & write" 두 선택지. |
| D3 | **가격 캡 강제 지점** | 1단계: WSB 견적 기준(F1). 2단계(선택): K8sCS 가 tx params 의 `max_price` 를 실제가와 비교해 초과 시 FAILED. 2단계는 K8sCS 변경이라 별도 PR, 트랙 B 범위 밖으로 두고 백로그. |
| D4 | **create_pod 의 `wait`** | v1 은 `wait: "none"` 고정(202 반환 후 모델이 `pods` 로 폴링). 서버 측 대기는 HTTP 타임아웃·MCP 클라이언트 타임아웃과 충돌. SDK 의 `wait_for_pod` 는 이미 있으니 CLI 는 `--wait` 지원. |
| D5 | **서빙 deploy 범위** | 등록된 모델(`registration_id`) 배포만. 모델 등록(HF 토큰, detect)은 v1 제외 — `next_step` 으로 콘솔 안내. |
| D6 | **task 제출의 스크립트 크기** | 256KB 상한(MCP 인자로 오는 본문). 그 이상은 Asset Hub 경유 안내. |

---

## 2. 작업 분해 (PR 단위, 전부 WSB dev 브랜치 → 사용자 real 머지)

### PR-B1 · WRITE 스코프와 발급 (작음)
- `MeshiveApiKeyScope.WRITE` + validator(CSV 에 write 허용).
- `POST /v1/user/api-keys`: `scopes: ["read"] | ["read","write"]`, write 포함 시 `expires_in_days` 필수·≤90(D1).
- `ApiKeyResponse` 그대로(이미 `scopes` 목록). 콘솔 `console/common/ApiKey.tsx` 에 스코프 라디오 + write 일 때 만료일 필수 UI (WebFrontend dev, 별도 PR).
- 테스트: 발급 파라미터 검증, write 키 만료 강제, read 키로 write 엔드포인트 403 메시지("lacks required scope: write" — MCP 가 이 문구로 `write_scope_required` 를 판별하므로 **문구 고정**).

### PR-B2 · 쓰기 라우터 골격 + 파드 생명주기 + 공통 인프라 (중간)
- `routers/sdk/write.py`(또는 `app.py` 분리): `require_api_key_scopes(WRITE)` + `rate_limit_k8s_*` 조합 의존성.
  주의: 현재 `rate_limit_k8s_*` 는 JWT 유저를 뽑는 의존성 — API 키 유저(email)를 받는 변형이 필요. `create_rate_limit_dependency` 가 user 추출을 인자로 받도록 리팩터.
- **멱등성**: `Idempotency-Key` 헤더(UUID). Redis `idem:{user}:{sha256(key+path)}` → 응답 본문·상태 24h. 같은 키 재요청은 저장 응답 재생, 처리 중이면 409 `in_progress`. POST/DELETE 전용. 헤더 없으면 그냥 처리.
- **클라이언트 식별**: `require_api_key_scopes` 에서 `X-Meshive-Client`·`User-Agent` 를 구조화 로그(user, key_id, client)로. 인가엔 미사용.
- 엔드포인트: `POST /v1/sdk/pods/{pod}/stop|start|restart`, `DELETE /v1/sdk/pods/{pod}` (`deleteLocalStoragePvNames`). 각각 기존 statefulset 핸들러 코루틴에 위임(읽기 라우터의 "확장 read 표면" 방식). `pod` 는 pod_name → statefulset_name 매핑 확인 필요(`-0` 접미).
- 테스트: 위임 호출 인자, 멱등성 재생, scope 403, 로그 필드.

### PR-B3 · 파드 견적 + 생성 (가장 큼, 위험 집중)
- `SdkPodCreateRequest`(계약 §2.1) → `_build_statefulset_request()`:
  1. 템플릿 해석: id 로 official/custom 조회(워크스페이스 멤버십 검증, 시크릿 env 는 서버 값 유지), `env`/`secret_keys` 덮어쓰기, `ports` 를 `endpoints` 에 병합(상한 검증), `command` → `overwrite_command`.
  2. 하드웨어: `gpu_model`+`gpu_vram_gb` 로 GpuDistribution 행 선택(재고 0 이면 409 `no_capacity`), `vcpu/ram_gb` 생략 시 `vcpu_recommended/ram_recommended`, CPU 템플릿이면 CpuDistribution.
  3. 가격: 웹과 같은 공식으로 `resource_price`, 볼륨/디스크로 `storage_price`(F1). 프리미엄 반영.
  4. 볼륨: `volumes[].storage` 를 PV 조회 → `VolumeCreateParam`(pv_name, storage_type, mount_path). 스팟+hostPath 는 기존 검증이 거른다.
  5. 이름 유일성: 같은 워크스페이스 활성 파드의 `user_alias` 와 중복 → 409.
  6. `max_price_per_hour_usd` < 견적 → 409 `price_exceeds_cap`(견적 포함).
- `POST /v1/sdk/pods/estimate`(read 스코프): 1~3·6 만 수행, 응답 = 계약 §2.1 estimate. `note: "Estimated from the current price table; the billed rate is fixed when the pod lands on a machine."`
- `POST /v1/sdk/pods`: 위 전체 → `statefulset.create` 핸들러 위임(가드·rate limit 포함). 응답 = 파드 단건 + 견적.
- 테스트: 빌더 단위 테스트(템플릿 병합, 가격 공식, 기본값, 상한), 재고 0, 이름 중복, 캡 초과, 스팟+hostPath.

### PR-B4 · 스토리지·서빙·태스크 (중간, 전부 위임)
- `POST /v1/sdk/storages`(→ pv create: name, size_gb, storage_type, disk_type, encrypted, region) / `DELETE /v1/sdk/storages/{pv}`(연결 파드 있으면 409 + 목록).
- `POST /v1/sdk/servings`(→ ServingDeployRequest) / `PATCH …/scale` / `PATCH …/pause` / `DELETE …/{id}`.
- `POST /v1/sdk/tasks`(→ TaskSubmitRequest, 스크립트 256KB 상한) / `POST …/{id}/stop`.
- 견적: 스토리지는 `DiskPriceTable`(GB·월), 서빙은 replica 단가×min~max, 태스크는 GPU 단가×max_duration 상한 — 각 create 의 `confirm=false` 응답용으로 WSB 가 계산.

### PR-B5 · 로그 + 목록 페이징 (읽기 확장, read 스코프)
- `GET /v1/sdk/pods/{pod}/logs?tail=200`: 스트림의 prefill 경로(K8sCS 로그 조회)만 떼어 단발 응답. 64KB 상한 + `truncated`.
- `GET /v1/sdk/tasks/{id}/logs?cursor=&limit=`: 기존 `logs/tail` 위임.
- (선택) pods/templates/servings/machines 목록에 `limit/cursor` — MCP 가 이미 잘라내므로 우선순위 낮음.

### SDK · meshive-python 0.1.0 (WSB PR-B2 이후 착수, B3~B5 따라 확장)
- `_client.py` 에 `_post/_patch/_delete` + `Idempotency-Key` 자동 생성(호출당 UUID, 자체 재시도에서만 재사용, GET 은 기존대로).
- 메서드 16개(계약 §4), 모델 `PodEstimate`, `Logs`. 예외: 409 → `ConflictError`(신규), 402 → `InsufficientCreditError`(신규) — MCP `translate` 와 코드 매핑 맞춤.
- CLI: `pod create/stop/start/restart/delete`, `storage create/delete`, `serving deploy/scale/pause/delete`, `task submit/stop`, `logs`. 돈·파괴 커맨드는 `--yes` 없으면 견적/요약 + 확인 프롬프트. `pod create --wait running`.
- `headers=` 생성자 인자(0.0.8 로 먼저 내도 됨 — MCP 의 private 접근 제거).
- CHANGELOG `## v0.1.0` 영어 사용자 관점, Docs `sdk-cli/*.mdx` 갱신.

### MCP · meshive-mcp (SDK 0.1.0 이후)
- 쓰기 도구 15개(계약 §2) + `logs`. 어노테이션 §0.8. `confirm` 규약은 **서버가 강제**(F10): confirm 누락/false → 견적 반환.
- `translate` 확장: 409 `conflict` 세분(`name_taken`, `no_capacity`, `price_exceeds_cap`, `in_progress`), 402.
- 테스트: 가짜 SDK 로 confirm 게이트·캡·견적 경로. dev 실측 시나리오: `gpus → estimate_pod → create_pod(confirm) → pods → logs → stop_pod → delete_pod(confirm)` 를 CPU 파드로(F9).
- 문서: README 도구 표, contract §2 확정본, 클라이언트 가이드에 "쓰기 키 만료" 안내.

---

## 3. 순서와 병렬성

```
PR-B1 ─► PR-B2 ─┬─► PR-B3 ─┐
                ├─► PR-B4 ─┼─► SDK 0.1.0 ─► MCP 쓰기 도구 ─► dev 실측 ─► real(사용자)
                └─► PR-B5 ─┘
```
- SDK 는 B2 가 dev 에 오르면 생명주기 메서드부터 시작해 B3~B5 를 따라간다(같은 계약 필드명이라 병렬 가능).
- FE 스코프 UI(PR-B1 짝)는 백엔드와 독립, WebFrontend dev.
- 각 PR 은 dev rollout 후 SDK/curl 로 확인, real 머지는 사용자.

## 4. 위험과 대응

| 위험 | 대응 |
|---|---|
| 견적 ≠ 청구가(노드별 cpu/ram 단가) | 응답에 "estimate" 명시, 캡은 견적 기준(D3), K8sCS 강제는 백로그. `pods` 조회에 실제 `price_per_hour` 가 오므로 create 응답의 `next_step` 에 "확정가는 pods 로 확인" 포함. |
| 템플릿 병합 실수(시크릿 env, is_base) | 빌더를 순수 함수로 두고 단위 테스트 집중. 웹이 보내는 페이로드를 fixture 로 캡처해 동일성 비교. |
| 이름/멱등성 우회로 이중 생성 | 이름 유일성 409 + Idempotency-Key 이중 방어. SDK 재시도는 키 재사용. |
| 쓰기 키 유출 | D1 만료 필수, 콘솔에서 즉시 폐기(기존 UI). `last_used_at`·client 로그로 추적. |
| dev 재고 부족으로 e2e 불가 | CPU 파드로 e2e, GPU 경로는 단위 테스트 + 견적 엔드포인트만 dev 검증. |
| rate limit 의존성이 JWT 전제 | B2 에서 user 추출을 주입식으로 리팩터. 기존 JWT 경로 동작 불변 테스트. |

## 5. 완료 기준
- WSB: `/v1/sdk` 쓰기 15 + estimate + logs 2, write 키 발급·검증, 멱등성, 클라이언트 로그. 테스트 통과, dev rollout.
- SDK 0.1.0: PyPI 배포, CHANGELOG, Docs.
- MCP: 27 도구, dev 실측 시나리오 통과(생성→로그→정지→삭제), 견적 없이 돈이 나가는 경로 없음.
