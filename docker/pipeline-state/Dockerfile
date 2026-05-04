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

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "src.main"]
