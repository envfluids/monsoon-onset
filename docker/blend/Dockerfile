# -----------------------------------------------------------------------------
# Monsoon Blend
# Model blending and visualization
# Target: Cloud Run
# -----------------------------------------------------------------------------

FROM python:3.11-slim

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
COPY docker/blend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY docker/blend/src/ ./src/

# Copy science scripts (unmodified) into expected directory structure
COPY blend/utils/ ./blend/utils/
COPY blend/data/ ./blend/data/

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
# Use non-interactive matplotlib backend
ENV MPLBACKEND=Agg

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
