"""
NCUM model configuration.

NCUM is an ensemble global NWP model, which is gridded on a 5/6 x 5/9 resolution.  The corresponding regridding weights map each
2-degree grid cell to subdistrict IDs.
"""

from pathlib import Path
from .config import ModelConfig

# Resolved relative to the blend/ directory (one level above utils/)
_BLEND_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent

NCUM_CONFIG = ModelConfig(
    label="ncmrwf",
    precip_var="precipitation_amount",
    is_ensemble=True,
    weights_file=_BLEND_DIR / "data" / "india2026" / "AIFS_NCUM_blend" / "data" / "coefs" / "subdistrict_ncum_weights.csv",
    date_offset_hours=0,
    skip_first_day=False,
    file_template="NCUM/output/precipitation_amount/precipitation_amount_{date}.nc",
    init_offset_hours=0,
)