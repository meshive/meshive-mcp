FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS build
COPY pyproject.toml README.md ./
COPY meshive_mcp ./meshive_mcp
# MESHIVE_SDK_SPEC 이 비어 있으면 PyPI 의 meshive 를, 아니면 그 스펙(예: "meshive @ git+https://github.com/meshive/meshive-python@dev")을
# 먼저 설치한다 — SDK 가 PyPI 에 오르기 전에 dev 이미지가 SDK dev 브랜치를 따라가게.
ARG MESHIVE_SDK_SPEC=""
RUN if [ -n "$MESHIVE_SDK_SPEC" ]; then pip install --prefix=/install "$MESHIVE_SDK_SPEC"; fi \
    && pip install --prefix=/install .

FROM base
COPY --from=build /install /usr/local
# 숫자 UID 로 지정 — k8s `runAsNonRoot` 는 이름(mcp)으로는 non-root 를 검증하지 못해 파드 생성이 실패한다.
RUN useradd --system --uid 10001 --no-create-home mcp
USER 10001:10001
ENV MESHIVE_MCP_HOST=0.0.0.0 MESHIVE_MCP_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"
CMD ["meshive-mcp", "--transport", "http"]
