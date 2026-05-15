# -----------------------------------------------------------------------------
# Monsoon GenCast
# GenCast model inference (JAX + GPU)
# Target: Cloud Batch
# -----------------------------------------------------------------------------

FROM nvcr.io/nvidia/jax:26.03-py3

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-dev \
    libeccodes-tools \
    cdo \
    nco \
    curl \
    bzip2 \
    lsb-release \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY docker/gencast/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY docker/gencast/src/ ./src/

# Copy science scripts (unmodified) into expected directory structure
COPY gencast/utils/ ./gencast/utils/
COPY gencast/data/ ./gencast/data/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
