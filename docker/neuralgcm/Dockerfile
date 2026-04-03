# -----------------------------------------------------------------------------
# Monsoon NeuralGCM
# NeuralGCM model inference (JAX + GPU)
# Target: Cloud Batch
# -----------------------------------------------------------------------------

FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies
COPY docker/neuralgcm/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY docker/neuralgcm/src/ ./src/

# Copy science scripts (unmodified) into expected directory structure
COPY NeuralGCM/utils/ ./NeuralGCM/utils/
COPY NeuralGCM/grids/ ./NeuralGCM/grids/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
