"""
Ethiopia weekly-mean precipitation forecast plots (AIFS / AIFS-ENS).

Entry point for the model_diagnostics pipeline:
    plot_precip(base, date, model)

Produces inside base/model_diagnostics/output/ethiopia/YYYYMMDDTHH/:
    precip_forecast_YYYYMMDDTHH.png   — 2×3 weekly-mean daily rainfall

Rows = AIFS / AIFS-ENS (mean);  Columns = Week 1 / Week 2 / Week 3.
"""

import logging
from datetime import datetime
from pathlib import Path

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from matplotlib.colors import ListedColormap

log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────

WEEKS = {
    "Week 1": (0, 6),
    "Week 2": (7, 13),
    "Week 3": (14, 20),
}

TP_VAR = "tp"
MEMBER_DIM = "number"
DAY_DIM = "day"
TIME_DIM = "time"

LON_MIN, LON_MAX = 33, 48
LAT_MIN, LAT_MAX = 3, 15

PRECIP_VMAX = 20  # mm/day colorbar max


# ── COLORMAP ──────────────────────────────────────────────────────────────────

def _get_precip_cmap():
    colors = [
        "#FFFFFF", "#C6FFFF", "#82FFFF", "#4CE6E6",
        "#00CCCC", "#00B2B2", "#00A000", "#1DB200",
        "#4CD600", "#99FF00", "#CCFF00", "#FFFF00",
        "#FFCC00", "#FF9900", "#FF0000", "#CC0000",
    ]
    return ListedColormap(colors, name="precip3_16lev")


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _parse_init_date(date: str) -> np.datetime64:
    y, mo, d, h = date[:4], date[4:6], date[6:8], date[9:11]
    return np.datetime64(f"{y}-{mo}-{d}T{h}:00", "h")


def _valid_period_str(init: np.datetime64, day_start: int, day_end: int) -> str:
    t0 = (init + np.timedelta64(day_start, "D")).astype("datetime64[D]")
    t1 = (init + np.timedelta64(day_end, "D")).astype("datetime64[D]")
    pretty = lambda t: datetime.strptime(str(t), "%Y-%m-%d").strftime("%-d %b")
    year = str(t1)[:4]
    return f"{pretty(str(t0))} – {pretty(str(t1))} {year}"


def _load_tp(nc_path: Path) -> xr.DataArray:
    ds = xr.open_dataset(nc_path)
    da = ds[TP_VAR]
    if TIME_DIM in da.dims:
        da = da.squeeze(TIME_DIM, drop=True)
    return da


def _weekly_mean(da: xr.DataArray, day_start: int, day_end: int) -> np.ndarray:
    subset = da.sel({DAY_DIM: slice(day_start, day_end)})
    if MEMBER_DIM in subset.dims:
        arr = subset.mean(dim=[MEMBER_DIM, DAY_DIM])
    else:
        arr = subset.mean(dim=DAY_DIM)
    return arr.transpose("lat", "lon").values.astype(float)


# ── PLOT ──────────────────────────────────────────────────────────────────────

def _make_figure(aifs_path: Path, ens_path: Path, init_date: np.datetime64,
                 date: str, save_path: Path):
    aifs_da = _load_tp(aifs_path)
    ens_da = _load_tp(ens_path)

    lats = aifs_da["lat"].values
    lons = aifs_da["lon"].values
    lon2d, lat2d = np.meshgrid(lons, lats)

    cmap = _get_precip_cmap()
    week_labels = list(WEEKS.keys())
    models = ["AIFS", "AIFS-ENS (mean)"]
    das = [aifs_da, ens_da]

    fig, axes = plt.subplots(
        nrows=2, ncols=3,
        figsize=(14, 8),
        subplot_kw={"projection": ccrs.PlateCarree()},
        gridspec_kw={"hspace": 0.08, "wspace": 0.04},
    )

    im = None
    for row, (model_label, da) in enumerate(zip(models, das)):
        for col, wlabel in enumerate(week_labels):
            d0, d1 = WEEKS[wlabel]
            ax = axes[row, col]
            data = _weekly_mean(da, d0, d1)
            period = _valid_period_str(init_date, d0, d1)

            im = ax.pcolormesh(
                lon2d, lat2d, data,
                cmap=cmap, shading="auto", vmin=0, vmax=PRECIP_VMAX,
                transform=ccrs.PlateCarree(),
            )
            ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], crs=ccrs.PlateCarree())
            ax.coastlines(resolution="10m", linewidth=0.8)
            ax.add_feature(cfeature.BORDERS, linewidth=0.4, edgecolor="gray")
            ax.gridlines(draw_labels=False, linewidth=0.3, color="gray", linestyle="--")

            if row == 0:
                ax.set_title(f"{wlabel}\n{period}", fontsize=10, pad=4)
            if col == 0:
                ax.text(-0.08, 0.5, model_label, transform=ax.transAxes,
                        fontsize=10, va="center", ha="right", rotation=90)

    cbar_ax = fig.add_axes([0.92, 0.15, 0.015, 0.7])
    cb = fig.colorbar(im, cax=cbar_ax)
    cb.set_label("Mean daily rainfall (mm/day)", fontsize=10)

    fig.suptitle(f"Ethiopia — Precipitation Forecast  |  Init: {date}",
                 fontsize=13, y=1.02)
    fig.savefig(save_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Saved: {save_path}")


# ── PUBLIC API ────────────────────────────────────────────────────────────────

def plot_precip(base: Path, date: str, model: str = None):
    """
    Called by model_diagnostics/utils/main.py.

    Parameters
    ----------
    base  : repo root (monsoon-onset/), passed in from main.py
    date  : YYYYMMDDTHH init date string
    model : "AIFS" or "AIFS_ENS" — accepted for API parity but both
            models are always plotted together (one row each)
    """
    aifs_nc = base / "AIFS" / "output" / "ethiopia" / "AIFS" / "tp" / f"tp_0p25_{date}.nc"
    ens_nc  = base / "AIFS" / "output" / "ethiopia" / "AIFS_ENS" / "tp" / f"tp_0p25_{date}.nc"
    out_dir = base / "model_diagnostics" / "output" / "ethiopia" / date
    out_dir.mkdir(parents=True, exist_ok=True)

    for p in (aifs_nc, ens_nc):
        if not p.exists():
            log.warning(f"Skipping {date}: {p} not found")
            return

    init_date = _parse_init_date(date)
    save_path = out_dir / f"precip_forecast_{date}.png"
    _make_figure(aifs_nc, ens_nc, init_date, date, save_path)
