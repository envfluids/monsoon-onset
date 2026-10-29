# -----------------------------------------------------------------------------
# Monsoon TPU Dispatch
# Cloud Run Job controller for Cloud TPU queued resources
# -----------------------------------------------------------------------------

# Pinned to the python:3.11-slim build today's image was made from
# (Python 3.11.17; published 2026-10-06), so a rebuild doesn't pick up a newer base.
FROM python:3.11-slim@sha256:0dd364ba7e10242f07755449e3a3d0e35f9efd987952737b90def6709ab0c5ce

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Exact versions (requirements.txt -> requirements.lock); --no-deps installs
# only what the lock lists.
COPY docker/tpu-dispatch/requirements.lock .
RUN pip install --no-cache-dir --no-deps -r requirements.lock

COPY docker/tpu-dispatch/src/ ./src/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "src.main"]
