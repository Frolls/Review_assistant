# syntax=docker/dockerfile:1.7

FROM python:3.13-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.6.10 /uv /uvx /bin/

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

RUN /app/.venv/bin/python -m spacy download ru_core_news_md

COPY README.md .env.example ./
COPY data ./data
COPY scripts ./scripts
COPY app ./app
COPY tests/eval ./tests/eval
COPY alembic.ini ./
COPY alembic ./alembic

RUN mkdir -p /app/var/ingestion && chown -R 1000:1000 /app/data /app/var

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --inexact

FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN useradd --create-home --uid 1000 appuser

COPY --from=builder --chown=appuser:appuser /app /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM builder AS tracing-builder

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra tracing --inexact

FROM runtime AS tracing

COPY --from=tracing-builder --chown=appuser:appuser /app/.venv /app/.venv

FROM tracing-builder AS eval-builder

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra eval --extra tracing --inexact

FROM runtime AS eval

COPY --from=eval-builder --chown=appuser:appuser /app/.venv /app/.venv
