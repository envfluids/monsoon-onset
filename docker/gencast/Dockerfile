# -----------------------------------------------------------------------------
# Monsoon GenCast
# GenCast model inference (JAX + TPU)
# Target: Cloud TPU VM queued resource
# -----------------------------------------------------------------------------

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    libeccodes-dev \
    libeccodes-tools \
    && curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg > /usr/share/keyrings/cloud.google.asc \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.asc] https://packages.cloud.google.com/apt gcsfuse-$(lsb_release -c -s) main" > /etc/apt/sources.list.d/gcsfuse.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gcsfuse \
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
