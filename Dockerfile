# One image for local and prod. Local builds pass UV_SYNC_ARGS="" to get the
# dev group (pytest, ruff); prod keeps --no-dev.
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

# The venv lives outside /app so the local bind mount of the repo cannot
# shadow it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG UV_SYNC_ARGS=--no-dev
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project $UV_SYNC_ARGS

COPY . .

# uid 1000 matches the usual host user, so files written through the local bind
# mount (pytest cache, new migrations) stay owned by them.
RUN useradd --uid 1000 --create-home app
USER app

EXPOSE 8000
# --workers 1: each worker would start its own scheduler and classifier loops.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
