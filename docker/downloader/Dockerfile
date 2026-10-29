# -----------------------------------------------------------------------------
# Monsoon Downloader
# Downloads ECMWF and NCEP data
# Target: Cloud Run
# -----------------------------------------------------------------------------

# Pinned to the python:3.11-slim build today's image was made from
# (Python 3.11.17; published 2026-10-06), so a rebuild doesn't pick up a newer base.
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libeccodes-dev \
    libeccodes-tools \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Exact versions (requirements.txt -> requirements.lock); --no-deps installs
# only what the lock lists.
COPY docker/downloader/requirements.lock .
RUN pip install --no-cache-dir --no-deps -r requirements.lock

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
