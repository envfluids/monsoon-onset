# -----------------------------------------------------------------------------
# Monsoon Sync
# Publishes outputs to GCS and optionally Google Drive
# Target: Cloud Run
# -----------------------------------------------------------------------------

# Pinned to the python:3.11-slim build today's image was made from
# (Python 3.11.17; published 2026-10-06), so a rebuild doesn't pick up a newer base.
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Exact versions (requirements.txt -> requirements.lock); --no-deps installs
# only what the lock lists.
COPY docker/sync/requirements.lock .
RUN pip install --no-cache-dir --no-deps -r requirements.lock

# Copy application code
COPY docker/sync/src/ ./src/

# Copy shared sync code and configuration
COPY sync/utils/ ./sync/utils/
COPY sync/config/ ./sync/config/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
