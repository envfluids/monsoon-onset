# -----------------------------------------------------------------------------
# Monsoon Pipeline-State Service
# Probes external IC sources and inspects GCS for per-stage completion state,
# returning a single JSON payload that drives workflow branching.
# -----------------------------------------------------------------------------

FROM python:3.11-slim

WORKDIR /app

COPY docker/pipeline-state/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/pipeline-state/src/ ./src/

# Copy the lightweight blend orchestrator config so pipeline-state can derive
# blend readiness from the same input contract the blend job executes.
RUN mkdir -p ./blend/utils
COPY blend/utils/main.py ./blend/utils/main.py
COPY blend/utils/__init__.py ./blend/utils/__init__.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "src.main"]
