# -----------------------------------------------------------------------------
# Monsoon DryWetCast-India job
# DryWetCast-India (pdasiitism/DryWetCast-India) pinned by full commit SHA and
# tarball checksum, run unmodified by the wrapper in src/.
# Target: Cloud Run job (CPU)
# -----------------------------------------------------------------------------

FROM python:3.11-slim-bookworm

WORKDIR /app

# libgomp1: OpenMP runtime that XGBoost's Linux wheel links against.
# libexpat1: needed by the GDAL bundled in rasterio's Linux wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libexpat1 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# DryWetCast-India at a fixed commit. Bumping it means changing both values.
ARG DRYWETCAST_COMMIT=56f4805a532718afd61176c85eec75fa8c46b137
ARG DRYWETCAST_SHA256=a77fb404bb26c1732e19aed3f2a11770cb1d8ec18670161af37cc8a7f39afe18
RUN curl -fsSL -o /tmp/drywetcast.tar.gz \
        "https://github.com/pdasiitism/DryWetCast-India/archive/${DRYWETCAST_COMMIT}.tar.gz" \
    && echo "${DRYWETCAST_SHA256}  /tmp/drywetcast.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/drywetcast \
    && tar -xzf /tmp/drywetcast.tar.gz -C /opt/drywetcast --strip-components=1 \
    && rm /tmp/drywetcast.tar.gz

# Exact versions (requirements.in -> requirements.lock, resolved for linux/x86_64).
COPY docker/drywetcast/requirements.lock .
RUN pip install --no-cache-dir --no-deps -r requirements.lock

# Fail the build if a native dependency (e.g. libgomp for XGBoost) is missing.
RUN cd /opt/drywetcast && python -c "\
import xgboost, rasterio, netCDF4, zarr, matplotlib; \
from pipeline import configs, aifs_features, gefs_features, ncmrwf_features, download_ncmrwf, inference; \
print('drywetcast imports ok, xgboost', xgboost.__version__)"

COPY docker/drywetcast/src/ ./src/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV DRYWETCAST_HOME=/opt/drywetcast
ENV DRYWETCAST_COMMIT=${DRYWETCAST_COMMIT}

# Last, so a new commit doesn't invalidate the layers above.
ARG CODE_VERSION=unknown
ENV CODE_VERSION=${CODE_VERSION}

ENTRYPOINT ["python", "-m", "src.main"]
