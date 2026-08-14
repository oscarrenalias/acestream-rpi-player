FROM ghcr.io/astral-sh/uv:0.10.8 AS uv

FROM python:3.13-alpine

WORKDIR /app

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock /app/
RUN uv sync --locked --no-install-project --no-dev

COPY catalog /app/catalog

ENV PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/catalog.db \
    PATH="/app/.venv/bin:$PATH"

VOLUME ["/data"]
EXPOSE 8090

CMD ["python", "-m", "catalog.app"]
