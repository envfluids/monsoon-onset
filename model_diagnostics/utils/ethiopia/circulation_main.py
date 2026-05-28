"""
Ethiopia wind circulation and convergence/divergence forecast plots.

Entry point for the model_diagnostics pipeline:
    plot_circulation(base, date, model)

Produces inside base/model_diagnostics/output/ethiopia/YYYYMMDDTHH/:
    wind_850_YYYYMMDDTHH.png    — 2x3 850 hPa wind speed + quivers
    wind_200_YYYYMMDDTHH.png    — 2x3 200 hPa wind speed + quivers
    divcon_850_YYYYMMDDTHH.png  — 2x3 850 hPa convergence/divergence + quivers
    divcon_200_YYYYMMDDTHH.png  — 2x3 200 hPa convergence/divergence + quivers

Rows = AIFS / AIFS-ENS (mean);  Columns = Week 1 / Week 2 / Week 3.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from matplotlib.colors import BoundaryNorm, ListedColormap
from scipy.ndimage import gaussian_filter

log = logging.getLogger(__name__)

# ── DOMAIN CONFIG ─────────────────────────────────────────────────────────────

DOMAINS = {
    "ethiopia": {
        "lon": (33, 48), "lat": (3, 15),
        "stride": 3, "div_qscale": 400, "label": "Ethiopia",
    },
    "africa": {
        "lon": (-26, 68), "lat": (-51, 40),
        "stride": 8, "div_qscale": 900, "label": "Africa",
    },
}

# ── WEEK / STEP CONFIG ────────────────────────────────────────────────────────

WEEKS = {"Week 1": (0, 6), "Week 2": (7, 13), "Week 3": (14, 20)}
STEPS_PER_DAY = 4   # 6-hourly steps

WIND_LEVELS = [("850", "u_850", "v_850"), ("200", "u_200", "v_200")]

# ── DISCRETE COLORMAPS ────────────────────────────────────────────────────────

_LEVELS_850 = [0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20]
_COLORS_850 = [
    "#FFFFFF", "#D5EEFF", "#90CCFF", "#4AB4F0",
    "#009EC8", "#00A880", "#20B848", "#70C820",
    "#C8D800", "#FFFF00",
]
CMAP_850 = ListedColormap(_COLORS_850, name="wind_850")
CMAP_850.set_over(_COLORS_850[-1])
NORM_850 = BoundaryNorm(_LEVELS_850, CMAP_850.N)

_LEVELS_200 = [0, 2, 4, 6, 8, 10, 15, 20, 30, 40, 50, 70]
_COLORS_200 = [
    "#0000BB", "#0040EE", "#0090FF", "#00C8D8",
    "#00A870", "#50C000", "#C8D800", "#FFB800",
    "#FF6000", "#CC0000", "#880000",
]
CMAP_200 = ListedColormap(_COLORS_200, name="wind_200")
CMAP_200.set_over(_COLORS_200[-1])
NORM_200 = BoundaryNorm(_LEVELS_200, CMAP_200.N)

_LEVELS_DIV = [-4, -3, -2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3, 4]
_COLORS_DIV = [
    "#004400", "#007700", "#30A830", "#70C870",
    "#B0DDB0", "#DDEEDD", "#EEDDC8", "#D0A060",
    "#B87030", "#904020", "#682010", "#401008",
]
CMAP_DIV_850 = ListedColormap(_COLORS_DIV, name="divcon")
CMAP_DIV_850.set_under(_COLORS_DIV[0])
CMAP_DIV_850.set_over(_COLORS_DIV[-1])
NORM_DIV_850 = BoundaryNorm(_LEVELS_DIV, CMAP_DIV_850.N)

_COLORS_DIV_200 = _COLORS_DIV[::-1]
CMAP_DIV_200 = ListedColormap(_COLORS_DIV_200, name="divcon_200")
CMAP_DIV_200.set_under(_COLORS_DIV_200[0])
CMAP_DIV_200.set_over(_COLORS_DIV_200[-1])
NORM_DIV_200 = BoundaryNorm(_LEVELS_DIV, CMAP_DIV_200.N)

_WIND_CMAPS = {
    "850": (CMAP_850, NORM_850, _LEVELS_850),
    "200": (CMAP_200, NORM_200, _LEVELS_200),
}

_DIV_CMAPS = {
    "850": (CMAP_DIV_850, NORM_DIV_850, _LEVELS_DIV),
    "200": (CMAP_DIV_200, NORM_DIV_200, _LEVELS_DIV),
}

# ── DATA LOADING ───────────────────────────────────────────────────────────────

def _load_ds(path: Path, lon_min: float, lon_max: float,
             lat_min: float, lat_max: float) -> xr.Dataset:
    if path.suffix == ".zarr":
        ds = xr.open_zarr(path, chunks={})
        if "prediction_timedelta" in ds.dims:
            ds = ds.rename({"prediction_timedelta": "step"})
    else:
        ds = xr.open_dataset(path, chunks={})
    if "time" in ds.dims:
        ds = ds.squeeze("time", drop=True)
    if float(ds.lat[0]) > float(ds.lat[-1]):
        ds = ds.isel(lat=slice(None, None, -1))
    if lon_min < 0 and float(ds.lon.min()) >= 0:
        ds = ds.assign_coords(lon=((ds.lon + 180) % 360) - 180).sortby("lon")
    return ds.sel(lat=slice(lat_min, lat_max), lon=slice(lon_min, lon_max))


def _step_slice(day_start: int, day_end: int) -> slice:
    return slice(day_start * STEPS_PER_DAY, (day_end + 1) * STEPS_PER_DAY)


def _weekly_wind(ds, u_var, v_var, day_start, day_end):
    sl = _step_slice(day_start, day_end)
    u = ds[u_var].isel(step=sl)
    v = ds[v_var].isel(step=sl)
    if "number" in u.dims:
        u, v = u.mean("number"), v.mean("number")
    u_m = u.mean("step").values.astype(float)
    v_m = v.mean("step").values.astype(float)
    return u_m, v_m, np.sqrt(u_m**2 + v_m**2)


def _divergence(u, v, lat, lon, smooth_sigma=0.0):
    if smooth_sigma > 0:
        u = gaussian_filter(u, sigma=smooth_sigma)
        v = gaussian_filter(v, sigma=smooth_sigma)
    R = 6_371_000.0
    cos_lat = np.cos(np.radians(lat))[:, np.newaxis]
    du_dlam = np.gradient(u, np.radians(lon), axis=1)
    dv_dphi = np.gradient(v, np.radians(lat), axis=0)
    return (du_dlam / (R * cos_lat) + dv_dphi / R) * 1e5

# ── PLOT HELPERS ───────────────────────────────────────────────────────────────

def _valid_period_str(date_str, day_start, day_end):
    y, mo, d, h = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]), int(date_str[9:11])
    init = datetime(y, mo, d, h)
    t0 = init + timedelta(days=day_start)
    t1 = init + timedelta(days=day_end)
    return f"{t0.strftime('%-d %b')} – {t1.strftime('%-d %b')} {t1.year}"


def _decorate_ax(ax, lon_min, lon_max, lat_min, lat_max):
    ax.set_extent([lon_min, lon_max, lat_min, lat_max], crs=ccrs.PlateCarree())
    ax.coastlines(resolution="10m", linewidth=0.8)
    ax.add_feature(cfeature.BORDERS, linewidth=0.4, edgecolor="gray")
    ax.gridlines(draw_labels=False, linewidth=0.3, color="gray", linestyle="--")


def _label_panels(axes, row, col, model_label, title):
    ax = axes[row, col]
    if row == 0:
        ax.set_title(title, fontsize=10, pad=4)
    if col == 0:
        ax.text(-0.08, 0.5, model_label, transform=ax.transAxes,
                fontsize=10, va="center", ha="right", rotation=90)


def _base_fig():
    return plt.subplots(
        2, 3, figsize=(14, 8),
        subplot_kw={"projection": ccrs.PlateCarree()},
        gridspec_kw={"hspace": 0.08, "wspace": 0.04},
    )

# ── FIGURE FUNCTIONS ───────────────────────────────────────────────────────────

def _wind_figure(
    aifs_nc,
    ens_nc,
    level,
    u_var,
    v_var,
    date_str,
    save_path,
    domain,
    model_labels,
):
    lon_min, lon_max = domain["lon"]
    lat_min, lat_max = domain["lat"]
    stride = domain["stride"]
    cmap, norm, levels = _WIND_CMAPS[level]
    vmax = levels[-1]

    aifs_ds = _load_ds(aifs_nc, lon_min, lon_max, lat_min, lat_max)
    ens_ds  = _load_ds(ens_nc,  lon_min, lon_max, lat_min, lat_max)
    lats, lons = aifs_ds.lat.values, aifs_ds.lon.values
    lon2d, lat2d = np.meshgrid(lons, lats)

    fig, axes = _base_fig()
    im = None
    for row, (label, ds) in enumerate(zip(model_labels, (aifs_ds, ens_ds))):
        for col, (wlabel, (d0, d1)) in enumerate(WEEKS.items()):
            ax = axes[row, col]
            u, v, ws = _weekly_wind(ds, u_var, v_var, d0, d1)
            im = ax.pcolormesh(lon2d, lat2d, ws, cmap=cmap, norm=norm,
                               shading="auto", transform=ccrs.PlateCarree())
            sl = slice(None, None, stride)
            ax.quiver(lon2d[sl, sl], lat2d[sl, sl], u[sl, sl], v[sl, sl],
                      transform=ccrs.PlateCarree(),
                      scale=vmax * 20, width=0.003, headwidth=3, color="black", alpha=0.75)
            _decorate_ax(ax, lon_min, lon_max, lat_min, lat_max)
            _label_panels(axes, row, col, label,
                          f"{wlabel}\n{_valid_period_str(date_str, d0, d1)}")

    cbar_ax = fig.add_axes([0.92, 0.15, 0.015, 0.7])
    fig.colorbar(im, cax=cbar_ax, extend="max", ticks=levels).set_label(
        f"{level} hPa wind speed (m/s)", fontsize=10)
    fig.suptitle(f"{domain['label']} — {level} hPa Wind Forecast  |  Init: {date_str}",
                 fontsize=13, y=1.02)
    fig.savefig(save_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Saved: {save_path}")


def _divcon_figure(
    aifs_nc,
    ens_nc,
    level,
    u_var,
    v_var,
    date_str,
    save_path,
    domain,
    model_labels,
    smooth_sigma=1.5,
):
    lon_min, lon_max = domain["lon"]
    lat_min, lat_max = domain["lat"]
    stride = domain["stride"]

    aifs_ds = _load_ds(aifs_nc, lon_min, lon_max, lat_min, lat_max)
    ens_ds  = _load_ds(ens_nc,  lon_min, lon_max, lat_min, lat_max)
    lats, lons = aifs_ds.lat.values, aifs_ds.lon.values
    lon2d, lat2d = np.meshgrid(lons, lats)

    cmap_div, norm_div, levels_div = _DIV_CMAPS[level]

    fig, axes = _base_fig()
    im = None
    for row, (label, ds) in enumerate(zip(model_labels, (aifs_ds, ens_ds))):
        for col, (wlabel, (d0, d1)) in enumerate(WEEKS.items()):
            ax = axes[row, col]
            u, v, _ = _weekly_wind(ds, u_var, v_var, d0, d1)
            div = _divergence(u, v, lats, lons, smooth_sigma)
            im = ax.pcolormesh(lon2d, lat2d, div, cmap=cmap_div, norm=norm_div,
                               shading="auto", transform=ccrs.PlateCarree())
            sl = slice(None, None, stride)
            ax.quiver(lon2d[sl, sl], lat2d[sl, sl], u[sl, sl], v[sl, sl],
                      transform=ccrs.PlateCarree(),
                      scale=domain["div_qscale"], width=0.003, headwidth=3,
                      color="black", alpha=0.6)
            _decorate_ax(ax, lon_min, lon_max, lat_min, lat_max)
            _label_panels(axes, row, col, label,
                          f"{wlabel}\n{_valid_period_str(date_str, d0, d1)}")

    cbar_ax = fig.add_axes([0.92, 0.15, 0.015, 0.7])
    fig.colorbar(im, cax=cbar_ax, extend="both", ticks=levels_div).set_label(
        f"{level} hPa divergence (×10⁻⁵ s⁻¹)\n← convergence  |  divergence →", fontsize=9)
    fig.suptitle(
        f"{domain['label']} — {level} hPa Convergence/Divergence  |  Init: {date_str}",
        fontsize=13, y=1.02)
    fig.savefig(save_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Saved: {save_path}")

# ── PUBLIC API ─────────────────────────────────────────────────────────────────

def plot_circulation(
    base: Path,
    date: str,
    deterministic_model: str = "AIFS_single_v1p1",
    ensemble_model: str = "AIFS_ENS_v1",
    output_dir: Path | None = None,
    smooth: float = 1.5,
):
    """
    Called by model_diagnostics/utils/main.py.

    Produces wind and convergence/divergence figures for both the Ethiopia
    and Africa domains. All output goes into the same ethiopia/<date>/ folder;
    Africa figures get an ``africa_`` filename prefix.

    Parameters
    ----------
    base   : repo root (monsoon-onset/), passed in from main.py
    date   : YYYYMMDDTHH init date string
    deterministic_model / ensemble_model : exact configured model names.
    smooth : Gaussian sigma applied to u,v before divergence computation
    """
    aifs_dir = base / "AIFS" / "output" / "raw" / deterministic_model
    ens_dir = base / "AIFS" / "output" / "raw" / ensemble_model
    out_dir = (
        Path(output_dir)
        if output_dir
        else base
        / "model_diagnostics"
        / "output"
        / "ethiopia"
        / date
        / f"{deterministic_model}_{ensemble_model}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    aifs_nc = aifs_dir / f"init_{date}.nc"
    ens_nc  = ens_dir  / f"init_{date}.zarr"
    if not ens_nc.exists():
        ens_nc = ens_dir / f"init_{date}.nc"

    for p in (aifs_nc, ens_nc):
        if not p.exists():
            log.warning(f"Skipping {date}: {p} not found")
            return

    for domain_name in ("ethiopia", "africa"):
        dom = DOMAINS[domain_name]
        prefix = f"{domain_name}_" if domain_name != "ethiopia" else ""
        for level, u_var, v_var in WIND_LEVELS:
            _wind_figure(aifs_nc, ens_nc, level, u_var, v_var,
                         date, out_dir / f"{prefix}wind_{level}_{date}.png", dom,
                         (deterministic_model, f"{ensemble_model} (mean)"))
            _divcon_figure(aifs_nc, ens_nc, level, u_var, v_var,
                           date, out_dir / f"{prefix}divcon_{level}_{date}.png", dom,
                           (deterministic_model, f"{ensemble_model} (mean)"), smooth)
