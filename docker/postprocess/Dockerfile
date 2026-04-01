# -----------------------------------------------------------------------------
# Monsoon Post-processor
# GCS verification gate — confirms model outputs exist before blending
# Target: Cloud Run
# -----------------------------------------------------------------------------

FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY docker/postprocess/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY docker/postprocess/src/ ./src/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
