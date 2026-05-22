"""
NGCM model configuration.

NGCM is a deterministic global NWP model, which for this test has already been regridded to 2-degree resolution.  The corresponding regridding weights map each
2-degree grid cell to subdistrict IDs.
"""

from pathlib import Path
from .config import ModelConfig

# Resolved relative to the blend/ directory (one level above utils/)
_BLEND_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent

NGCM_CONFIG = ModelConfig(
    label="ngcm",
    precip_var="tp",
    is_ensemble=True,
    weights_file=_BLEND_DIR / "data" / "india2026" / "AIFS_NGCM_blend" / "data" / "coefs" / "subdistrict_ngcm_weights.csv",
    date_offset_hours=0,
    skip_first_day=False,
    file_template="NeuralGCM/output/india/tp/tp_2p0_{date}.nc",
    init_offset_hours=0,
)
