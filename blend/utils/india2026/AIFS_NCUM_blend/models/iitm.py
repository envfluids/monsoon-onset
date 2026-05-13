"""
IITM-ERP model configuration.

IITM-ERP is an ensemble global NWP model, which this pipeline assumes has been regridded to 2-degree resolution.  The corresponding regridding weights map each 2-degree grid cell to subdistrict IDs. 
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
