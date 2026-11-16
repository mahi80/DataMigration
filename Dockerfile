FROM python:3.12-slim
ARG INSTALL_EXTRAS=api,postgres,mongo,redis,delta,files,queries,mcp,aws,azure,databricks
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MIGRATION_STATE_DIR=/state \
    UV_PYTHON_DOWNLOADS=never PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
RUN python -m pip install --no-cache-dir 'uv==0.13.6' \
    && set -- \
    && for extra in $(printf '%s' "$INSTALL_EXTRAS" | tr ',' ' '); do set -- "$@" --extra "$extra"; done \
    && uv sync --frozen --no-dev --no-editable --no-cache "$@" \
    && groupadd --gid 10001 migration \
    && useradd --uid 10001 --gid 10001 --create-home migration \
    && mkdir -p /state \
    && chown 10001:10001 /state
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["datamigrate", "serve", "--host", "0.0.0.0", "--port", "8000"]
