# -----------------------------------------------------------------------------
# Monsoon AIFS
# ECMWF AIFS model inference (PyTorch + GPU)
# Target: Cloud Batch with GPU
# -----------------------------------------------------------------------------

# Use NVIDIA PyTorch base image with CUDA
FROM nvcr.io/nvidia/pytorch:26.01-py3

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-dev \
    libeccodes-tools \
    cdo \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY docker/aifs/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY docker/aifs/src/ ./src/

# Copy science scripts (unmodified) into the expected utils/ directory structure
COPY AIFS/utils/ ./AIFS/utils/
COPY AIFS/grids/ ./AIFS/grids/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
