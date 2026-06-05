"""
IITM-ERP model configuration.

IITM-ERP is an ensemble global NWP model at native 0.25-degree resolution.
The GRIB files are converted to NetCDF using convert_iitm_to_netcdf.py which:
  - reads the raw GRIB at native 0.25 degree resolution
  - subsets to the India domain (lat 8.0-37.25, lon 68.0-97.50)
  - writes a pipeline-ready NetCDF with 18 ensemble members

The corresponding regridding weights (subdistrict_iitm_weights.csv) map each
0.25-degree grid cell to subdistrict IDs.
"""

from pathlib import Path
from .config import ModelConfig

# Resolved relative to the blend/ directory (one level above utils/)
_BLEND_DIR = Path(__file__).resolve().parent.parent.parent

IITM_CONFIG = ModelConfig(
    label="iitm",
    precip_var="tp",
    is_ensemble=True,
    weights_file=_BLEND_DIR / "data" / "coefs" / "subdistrict_iitm_weights.csv",
    date_offset_hours=0,
    skip_first_day=False,
    file_template="IITM/output/tp/tp_{date}.nc",
    init_offset_hours=0,
)
