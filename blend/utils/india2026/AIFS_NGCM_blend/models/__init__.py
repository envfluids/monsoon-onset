"""
Model configurations for the blend pipeline.

Import the config objects you need and add them to ACTIVE_MODELS in main.py.
"""

from .config import ModelConfig
from .aifs import AIFS_CONFIG
from .ngcm import NGCM_CONFIG
from .iitm import IITM_CONFIG
from .ncum import NCUM_CONFIG

__all__ = ["ModelConfig", "AIFS_CONFIG", "NGCM_CONFIG", "IITM_CONFIG", "NCUM_CONFIG"]
