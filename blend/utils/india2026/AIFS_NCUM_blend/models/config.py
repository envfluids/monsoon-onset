"""
ModelConfig: per-model configuration for the blend pipeline.

To add a new forecast model:
1. Create a new file in utils/models/, e.g. utils/models/myfcst.py
2. Define a ModelConfig instance with the parameters below
3. Import it in utils/models/__init__.py
4. Add it to ACTIVE_MODELS in utils/main.py

All models are processed by the same generic function in utils/process_forecast.py.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ModelConfig:
    """Configuration for one forecast model.

    Parameters
    ----------
    label : str
        Short name used as column prefix in outputs.
        e.g. "aifs" → "aifs_rain_daily", "diff_aifs_week1"
    precip_var : str
        Name of the precipitation variable in the model's NetCDF file.
        e.g. "tp" for AIFS/NeuralGCM
    is_ensemble : bool
        True if the NetCDF has an ensemble-member dimension.
        Expected dimension order when True:  [day, ens, time, lat, lon]
        Expected dimension order when False: [day, time, lat, lon]
        For ensemble models the ensemble mean is taken before onset detection
        (individual members are NOT used for onset probability).
    weights_file : Path
        Path to the conservative-regridding weights CSV for this model's grid.
        Required columns: target_id, source_id, weight
          - target_id : integer subdistrict ID
          - source_id : grid-cell string "lat_lon" formatted to 2 decimal places,
                        e.g. "10.00_77.25"
          - weight    : regridding weight (weights within each target_id sum to 1)
        Each grid resolution needs its own weights file.
    date_offset_hours : int
        Hours added to the decoded forecast initialization times after reading
        from the NetCDF.  Use 12 for AIFS (which stores times at 12Z of the
        previous day); use 0 for most other models.
    skip_first_day : bool
        If True, drop index 0 along the day axis before processing and
        decrement remaining day values by 1.
        AIFS-specific: the first day is a repeated initialization time-step
        that should be discarded before computing daily totals.
    """

    label: str
    precip_var: str
    is_ensemble: bool
    weights_file: Path
    # Deprecated: no longer used. The forecast-date label now comes from the
    # pipeline date passed to process_forecast(), so offsets against the
    # NetCDF's internal time coord are unnecessary. Retained for back-compat.
    date_offset_hours: int = 0
    skip_first_day: bool = False
    # Path template for the forecast NetCDF, relative to the repo base directory.
    # Use '{date}' as the placeholder for the file's init-date string (YYYYMMDDTHH).
    # Example: "AIFS/output/tp/tp_{date}.nc"
    file_template: str = ""
    # Hours subtracted from the requested pipeline date to derive the file's
    # init-date string. AIFS is produced at 12Z of the previous day, so it uses 12.
    # Most models should leave this at 0.
    init_offset_hours: int = 0
