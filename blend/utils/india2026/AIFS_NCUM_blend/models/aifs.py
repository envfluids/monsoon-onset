"""
AIFS model configuration.

AIFS (ECMWF AI Forecasting System) is a deterministic global NWP model at
0.25-degree resolution.  The corresponding regridding weights map each
0.25-degree grid cell to subdistrict IDs.

To swap AIFS for a different model resolution, point weights_file at the
appropriate weights CSV and set precip_var to match the NetCDF variable name.
"""

from pathlib import Path
from .config import ModelConfig

# Resolved relative to the blend/ directory (one level above utils/)
_BLEND_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent

AIFS_CONFIG = ModelConfig(
    label="aifs",
    precip_var="tp",
    is_ensemble=False,
    weights_file=_BLEND_DIR / "data" / "india2026" / "AIFS_NCUM_blend" / "data" / "coefs" / "subdistrict_0p25deg_weights.csv",
    date_offset_hours=0,
    skip_first_day=True,
    file_template="AIFS/output/india/tp/tp_0p25_{date}.nc",
    init_offset_hours=0,
)