FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS build
COPY pyproject.toml README.md ./
COPY meshive_mcp ./meshive_mcp
# MESHIVE_SDK_SPEC 이 비어 있으면 PyPI 의 meshive 를, 아니면 그 스펙(예: "meshive @ git+https://github.com/meshive/meshive-python@dev")을
# 먼저 설치한다 — SDK 가 PyPI 에 오르기 전에 dev 이미지가 SDK dev 브랜치를 따라가게.
ARG MESHIVE_SDK_SPEC=""
# git 은 spec 이 git+ 일 때만 필요하다(빌드 스테이지에만 설치, 런타임 이미지에는 없다).
# 두 번에 나눠 설치하면 두 번째 pip 이 --prefix 아래를 못 보고 PyPI 에서 meshive 를 다시 찾는다 → 한 호출에 같이 넘긴다.
RUN if [ -n "$MESHIVE_SDK_SPEC" ]; then \
        apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
        && pip install --prefix=/install "$MESHIVE_SDK_SPEC" .; \
    else \
        pip install --prefix=/install .; \
    fi

FROM base
# 어떤 MCP 커밋이 어떤 SDK 커밋으로 도는지 — 이미지 라벨과 /healthz(revision, sdk_revision) 양쪽에서 읽는다(리뷰 C2).
# CI 가 채운다(ci.yml). 로컬 빌드는 비어 있고 /healthz 는 null 로 답한다.
ARG MESHIVE_MCP_REVISION=""
ARG MESHIVE_SDK_REVISION=""
LABEL org.opencontainers.image.source="https://github.com/meshive/meshive-mcp" \
      org.opencontainers.image.revision="$MESHIVE_MCP_REVISION" \
      ai.meshive.sdk.revision="$MESHIVE_SDK_REVISION"
ENV MESHIVE_MCP_REVISION="$MESHIVE_MCP_REVISION" MESHIVE_SDK_REVISION="$MESHIVE_SDK_REVISION"
COPY --from=build /install /usr/local
# 숫자 UID 로 지정 — k8s `runAsNonRoot` 는 이름(mcp)으로는 non-root 를 검증하지 못해 파드 생성이 실패한다.
RUN useradd --system --uid 10001 --no-create-home mcp
USER 10001:10001
ENV MESHIVE_MCP_HOST=0.0.0.0 MESHIVE_MCP_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"
CMD ["meshive-mcp", "--transport", "http"]
