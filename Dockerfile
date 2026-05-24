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

# Install dotenvx for encrypted secrets support
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -sfS https://dotenvx.sh | sh \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Copy virtual environment and source from builder
COPY --from=builder /app/.venv .venv
COPY --from=builder /app/src src

# Copy default config for reference
COPY config.example.toml ./

# Copy encrypted vault if present (decrypted at runtime via DOTENV_KEY)
COPY .env.vault* ./

# Data directory for SQLite databases
RUN mkdir -p /app/data

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')" || exit 1

# Use dotenvx to decrypt .env.vault at runtime (requires DOTENV_KEY env var).
# Falls back to plain `python -m palmtop` if no vault or key present.
CMD ["sh", "-c", "if [ -f .env.vault ] && command -v dotenvx >/dev/null 2>&1; then dotenvx run -- python -m palmtop; else python -m palmtop; fi"]
