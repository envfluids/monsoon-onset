# -----------------------------------------------------------------------------
# Monsoon NeuralGCM
# NeuralGCM model inference (JAX + GPU)
# Target: Cloud Batch
# -----------------------------------------------------------------------------

FROM nvcr.io/nvidia/jax:26.03-py3

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-dev \
    libeccodes-tools \
    cdo \
    nco \
    && rm -rf /var/lib/apt/lists/*

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
