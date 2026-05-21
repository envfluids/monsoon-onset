# -----------------------------------------------------------------------------
# Monsoon GenCast
# GenCast model inference (JAX + TPU)
# Target: Cloud TPU VM queued resource
# -----------------------------------------------------------------------------

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libeccodes-dev \
    libeccodes-tools \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies into /app/.venv. The project itself is not
# installed because the container copies runtime modules into /app/src and
# /app/gencast after dependency resolution.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY docker/gencast/pyproject.toml ./
RUN uv sync --no-dev --no-install-project

# Copy application code
COPY docker/gencast/src/ ./src/

# Copy science scripts (unmodified) into expected directory structure
COPY gencast/utils/ ./gencast/utils/
COPY gencast/data/ ./gencast/data/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PATH="/app/.venv/bin:${PATH}"

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
