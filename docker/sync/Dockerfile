# -----------------------------------------------------------------------------
# Monsoon Sync
# Publishes outputs to GCS and optionally Google Drive
# Target: Cloud Run
# -----------------------------------------------------------------------------

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY docker/sync/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

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
