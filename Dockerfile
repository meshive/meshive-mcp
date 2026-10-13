FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS build
COPY pyproject.toml README.md ./
COPY meshive_mcp ./meshive_mcp
# If MESHIVE_SDK_SPEC is empty, install meshive from PyPI; otherwise install that spec (e.g. "meshive @ git+https://github.com/meshive/meshive-python@dev")
# first — so dev images follow the SDK dev branch before the SDK is on PyPI.
ARG MESHIVE_SDK_SPEC=""
# git is needed only when the spec is git+ (installed in the build stage only, not in the runtime image).
# Installing in two steps makes the second pip miss what's under --prefix and look up meshive on PyPI again → pass both in one call.
RUN if [ -n "$MESHIVE_SDK_SPEC" ]; then \
        apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* \
        && pip install --prefix=/install "$MESHIVE_SDK_SPEC" .; \
    else \
        pip install --prefix=/install .; \
    fi

FROM base
# Which MCP commit runs with which SDK commit — readable from both the image labels and /healthz (revision, sdk_revision).
# Filled in by CI (ci.yml). Empty in local builds, where /healthz answers null.
ARG MESHIVE_MCP_REVISION=""
ARG MESHIVE_SDK_REVISION=""
LABEL org.opencontainers.image.source="https://github.com/meshive/meshive-mcp" \
      org.opencontainers.image.revision="$MESHIVE_MCP_REVISION" \
      ai.meshive.sdk.revision="$MESHIVE_SDK_REVISION"
ENV MESHIVE_MCP_REVISION="$MESHIVE_MCP_REVISION" MESHIVE_SDK_REVISION="$MESHIVE_SDK_REVISION"
COPY --from=build /install /usr/local
# Use a numeric UID — k8s `runAsNonRoot` can't verify non-root from a name (mcp), so pod creation fails.
RUN useradd --system --uid 10001 --no-create-home mcp
USER 10001:10001
ENV MESHIVE_MCP_HOST=0.0.0.0 MESHIVE_MCP_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"
CMD ["meshive-mcp", "--transport", "http"]
