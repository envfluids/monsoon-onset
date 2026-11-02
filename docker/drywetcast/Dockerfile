# -----------------------------------------------------------------------------
# Monsoon DryWetCast-India job
# DryWetCast-India (pdasiitism/DryWetCast-India) pinned by full commit SHA and
# tarball checksum, run unmodified by the wrapper in src/.
# Target: Cloud Run job (CPU)
#
# The version comes from products/drywetcast/pins.yaml (one image per role), so
# the build arguments are required. Local build of the live version:
#   docker build $(.venv/bin/python scripts/drywetcast_pins.py build-args live) \
#     -f docker/drywetcast/Dockerfile .
# -----------------------------------------------------------------------------

# Pinned to the python:3.11-slim-bookworm build today's live image was made from
# (Python 3.11.18; published 2026-10-26), so a rebuild doesn't pick up a newer base.
FROM python:3.11-slim-bookworm@sha256:90d117a7f1ef6535dc75831e1a9184725757fede0d4d424d7977fb5429d50610

WORKDIR /app

# libgomp1: OpenMP runtime that XGBoost's Linux wheel links against.
# libexpat1: needed by the GDAL bundled in rasterio's Linux wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libexpat1 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# DryWetCast-India at a fixed commit, checked against the tarball checksum.
ARG DRYWETCAST_REPO
ARG DRYWETCAST_COMMIT
ARG DRYWETCAST_SHA256
RUN { test -n "${DRYWETCAST_REPO}" && test -n "${DRYWETCAST_COMMIT}" && test -n "${DRYWETCAST_SHA256}"; } \
        || { echo "Set the DryWetCast build arguments (scripts/drywetcast_pins.py build-args <role>)" >&2; exit 1; } \
    && curl -fsSL -o /tmp/drywetcast.tar.gz \
        "https://github.com/${DRYWETCAST_REPO}/archive/${DRYWETCAST_COMMIT}.tar.gz" \
    && echo "${DRYWETCAST_SHA256}  /tmp/drywetcast.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/drywetcast \
    && tar -xzf /tmp/drywetcast.tar.gz -C /opt/drywetcast --strip-components=1 \
    && rm /tmp/drywetcast.tar.gz

# Exact versions (requirements.in -> requirements.lock, resolved for linux/x86_64);
# the lock file is the role's entry in pins.yaml.
ARG DRYWETCAST_LOCK=docker/drywetcast/requirements.lock
COPY ${DRYWETCAST_LOCK} requirements.lock
RUN pip install --no-cache-dir --no-deps -r requirements.lock

# Fail the build if a native dependency (e.g. libgomp for XGBoost) is missing.
RUN cd /opt/drywetcast && python -c "\
import xgboost, rasterio, netCDF4, zarr, matplotlib, pandas, geopandas, pyogrio, shapely, pyproj; \
from pipeline import configs, aifs_features, gefs_features, ncmrwf_features, download_ncmrwf, inference, outlook, subdistrict; \
print('drywetcast imports ok, xgboost', xgboost.__version__)"

COPY docker/drywetcast/src/ ./src/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV DRYWETCAST_HOME=/opt/drywetcast
ENV DRYWETCAST_COMMIT=${DRYWETCAST_COMMIT}

# Which pins.yaml role the image was built for (live, fallback or candidate).
ARG DRYWETCAST_ROLE=unknown
ENV DRYWETCAST_ROLE=${DRYWETCAST_ROLE}

# Last, so a new commit doesn't invalidate the layers above.
ARG CODE_VERSION=unknown
ENV CODE_VERSION=${CODE_VERSION}

ENTRYPOINT ["python", "-m", "src.main"]
