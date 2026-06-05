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
    curl \
    bzip2 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV MAMBA_ROOT_PREFIX=/opt/micromamba

RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest \
    | tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba \
    && micromamba create -y -n ncl -c conda-forge ncl \
    && micromamba clean -a -y \
    && printf '%s\n' \
        '#!/usr/bin/env bash' \
        'export NCARG_ROOT=/opt/micromamba/envs/ncl' \
        'exec /opt/micromamba/envs/ncl/bin/ncl "$@"' \
        > /usr/local/bin/ncl \
    && chmod +x /usr/local/bin/ncl \
    && ncl -V

# Install Python dependencies
COPY docker/neuralgcm/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY docker/neuralgcm/src/ ./src/

# Copy science scripts (unmodified) into expected directory structure
COPY NeuralGCM/utils/ ./NeuralGCM/utils/
COPY NeuralGCM/grids/ ./NeuralGCM/grids/
COPY NeuralGCM/data/ ./NeuralGCM/data/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
