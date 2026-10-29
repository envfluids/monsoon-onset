# -----------------------------------------------------------------------------
# Monsoon Pipeline-State Service
# Probes external IC sources and inspects GCS for per-stage completion state,
# returning a single JSON payload that drives workflow branching.
# -----------------------------------------------------------------------------

# Pinned to the python:3.11-slim build today's image was made from
# (Python 3.11.17; published 2026-10-06), so a rebuild doesn't pick up a newer base.
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

WORKDIR /app

# Exact versions (requirements.txt -> requirements.lock); --no-deps installs
# only what the lock lists.
COPY docker/pipeline-state/requirements.lock .
RUN pip install --no-cache-dir --no-deps -r requirements.lock

COPY docker/pipeline-state/src/ ./src/

# Copy the lightweight blend orchestrator config so pipeline-state can derive
# blend readiness from the same input contract the blend job executes.
RUN mkdir -p ./blend/utils
COPY blend/utils/main.py ./blend/utils/main.py
COPY blend/utils/__init__.py ./blend/utils/__init__.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "src.main"]
