FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS build
COPY pyproject.toml README.md ./
COPY meshive_mcp ./meshive_mcp
RUN pip install --prefix=/install .

FROM base
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --no-create-home mcp
USER mcp
ENV MESHIVE_MCP_HOST=0.0.0.0 MESHIVE_MCP_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"
CMD ["meshive-mcp", "--transport", "http"]
