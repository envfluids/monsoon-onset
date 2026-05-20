# -----------------------------------------------------------------------------
# Monsoon TPU Dispatch
# Cloud Run Job controller for Cloud TPU queued resources
# -----------------------------------------------------------------------------

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY docker/tpu-dispatch/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/tpu-dispatch/src/ ./src/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "src.main"]
