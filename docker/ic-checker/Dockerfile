# -----------------------------------------------------------------------------
# Monsoon IC Checker
# Lightweight HTTP service that probes remote 00z initial-condition availability
# -----------------------------------------------------------------------------

FROM python:3.11-slim

WORKDIR /app

COPY docker/ic-checker/src/ ./src/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

ENTRYPOINT ["python", "-m", "src.main"]
