# -----------------------------------------------------------------------------
# Monsoon AIFS-ENS v2
# ECMWF AIFS ensemble v2 model inference (PyTorch + GPU)
# Target: Cloud Batch with GPU
# -----------------------------------------------------------------------------

# Use NVIDIA PyTorch base image with CUDA, pinned to the nvcr.io/nvidia/pytorch:25.08-py3
# build today's image was made from (PyTorch 2.8.0a0+34c6371, CUDA 13.0), so a rebuild
# doesn't pick up a newer base.
FROM nvcr.io/nvidia/pytorch:25.08-py3@sha256:ace9a848c0ae543317e3c4763b6b4248961c47902625abfe3c77a0fb931c50fb

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-dev \
    libeccodes-tools \
    cdo \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies: exact versions of what pip adds to the base
# (requirements.txt -> requirements.lock).
# --no-deps: only what the lock lists, so pip never touches the base's torch or numpy.
# --no-build-isolation: the two sdists build with the base's setuptools/wheel, not the latest.
COPY docker/aifs-ens-v2/requirements.lock .
RUN pip install --no-cache-dir --no-deps --no-build-isolation -r requirements.lock

# Copy application code
COPY docker/aifs-ens-v2/src/ ./src/

# Copy science scripts (unmodified) into the expected utils/ directory structure
COPY AIFS/utils/ ./AIFS/utils/
COPY AIFS/grids/ ./AIFS/grids/
COPY AIFS/data/ ./AIFS/data/
COPY config/ ./config/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV MODEL=AIFS_ENS_v2

# Build commit, recorded in the India store's code_version attribute. Last, so a
# new commit doesn't invalidate the cached layers above.
ARG AIFS_CODE_VERSION=unknown
ENV AIFS_CODE_VERSION=${AIFS_CODE_VERSION}

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
