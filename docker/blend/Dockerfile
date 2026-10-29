# -----------------------------------------------------------------------------
# Monsoon Blend
# Model blending and visualization
# Target: Cloud Run
# -----------------------------------------------------------------------------

# Pinned to the python:3.11-slim build today's image was made from
# (Python 3.11.17; published 2026-10-06), so a rebuild doesn't pick up a newer base.
FROM python:3.11-slim@sha256:0dd364ba7e10242f07755449e3a3d0e35f9efd987952737b90def6709ab0c5ce

WORKDIR /app

# Install system dependencies for geopandas and matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    gdal-bin \
    libffi-dev \
    # For matplotlib
    libfreetype6-dev \
    libpng-dev \
    # For fonts
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Set GDAL environment variables
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal

# Install Python dependencies
# Exact versions (requirements.txt -> requirements.lock); --no-deps installs
# only what the lock lists.
COPY docker/blend/requirements.lock .
RUN pip install --no-cache-dir --no-deps -r requirements.lock

# Copy application code
COPY docker/blend/src/ ./src/

# Copy science scripts (unmodified) into expected directory structure
COPY blend/utils/ ./blend/utils/
COPY model_diagnostics/utils/ ./model_diagnostics/utils/
COPY blend/data/support/ ./blend/data/support/
COPY blend/data/india_shapefile/ ./blend/data/india_shapefile/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
# Use non-interactive matplotlib backend
ENV MPLBACKEND=Agg

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
