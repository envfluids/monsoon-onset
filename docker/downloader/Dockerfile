# -----------------------------------------------------------------------------
# Monsoon Downloader
# Downloads ECMWF and NCEP data
# Target: Cloud Run
# -----------------------------------------------------------------------------

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libeccodes-dev \
    libeccodes-tools \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY docker/downloader/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY docker/downloader/src/ ./src/

# Copy science scripts (unmodified) into expected utils/ directories
COPY IC/utils/ ./IC/utils/
COPY config/ ./config/
COPY gencast/utils/download_sst.py ./gencast/utils/download_sst.py

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Entry point
ENTRYPOINT ["python", "-m", "src.main"]
