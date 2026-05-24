# Palmtop — self-hosted AI agent platform
# Build:  docker build -t palmtop .
# Run:    docker compose up

FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (cache-friendly layer)
COPY pyproject.toml uv.lock ./

# Install production dependencies (no dev, no local llama.cpp)
RUN uv sync --frozen --no-dev --extra cloud --extra telegram --extra web --no-install-project

# Copy source code
COPY src/ src/

# Install the project itself
RUN uv sync --frozen --no-dev --extra cloud --extra telegram --extra web


# ── Runtime stage ──────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy virtual environment and source from builder
COPY --from=builder /app/.venv .venv
COPY --from=builder /app/src src

# Copy default config for reference
COPY config.example.toml ./

# Data directory for SQLite databases
RUN mkdir -p /app/data

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')" || exit 1

CMD ["python", "-m", "palmtop"]
