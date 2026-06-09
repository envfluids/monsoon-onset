import logging
from datetime import datetime, timedelta
from pathlib import Path

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import cartopy.io.shapereader as shpreader
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, ListedColormap
from scipy.ndimage import gaussian_filter

log = logging.getLogger(__name__)

# ── DOMAIN CONFIG ─────────────────────────────────────────────────────────────

DOMAINS = {
    "ethiopia": {
        "lon": (33, 48),
        "lat": (3, 15),
        "stride": 3,
        "div_qscale": 400,
        "aqs": 25,  # anomaly-wind quiver scale multiplier (scale = anom_vmax * aqs)
        "label": "Ethiopia",
    },
    "africa": {
        "lon": (-26, 73),
        "lat": (-56, 40),
        "stride": 8,
        "div_qscale": 900,
        "aqs": 60,
        "label": "Africa",
    },
    "mslp_extended": {
        "lon": (-26, 73),
        "lat": (-56, 40),
        "stride": 10,
        "div_qscale": 1000,
        "aqs": 65,
        "label": "Africa & Indian Ocean",
    },
}

# Default climatology produced by build_climatology.py.
CLIM_FILENAME = "era5_clim_africa_1990-2019.zarr"

# ── WEEK / STEP CONFIG ────────────────────────────────────────────────────────

WEEKS = {"Week 1": (0, 6), "Week 2": (7, 13), "Week 3": (14, 20)}
DEFAULT_STEPS_PER_DAY = 4  # 6-hourly steps

WIND_LEVELS = [("850", "u_850", "v_850"), ("200", "u_200", "v_200")]

# ── DISCRETE COLORMAPS ────────────────────────────────────────────────────────

_LEVELS_850 = [0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20]
_COLORS_850 = [
    "#FFFFFF",
    "#D5EEFF",
    "#90CCFF",
    "#4AB4F0",
    "#009EC8",
    "#00A880",
    "#20B848",
    "#70C820",
    "#C8D800",
    "#FFFF00",
]
CMAP_850 = ListedColormap(_COLORS_850, name="wind_850")
CMAP_850.set_over(_COLORS_850[-1])
NORM_850 = BoundaryNorm(_LEVELS_850, CMAP_850.N)

_LEVELS_200 = [0, 2, 4, 6, 8, 10, 15, 20, 30, 40, 50, 70]
_COLORS_200 = [
    "#0000BB",
    "#0040EE",
    "#0090FF",
    "#00C8D8",
    "#00A870",
    "#50C000",
    "#C8D800",
    "#FFB800",
    "#FF6000",
    "#CC0000",
    "#880000",
]
CMAP_200 = ListedColormap(_COLORS_200, name="wind_200")
CMAP_200.set_over(_COLORS_200[-1])
NORM_200 = BoundaryNorm(_LEVELS_200, CMAP_200.N)

_LEVELS_DIV = [-4, -3, -2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3, 4]
_COLORS_DIV = [
    "#004400",
    "#007700",
    "#30A830",
    "#70C870",
    "#B0DDB0",
    "#DDEEDD",
    "#EEDDC8",
    "#D0A060",
    "#B87030",
    "#904020",
    "#682010",
    "#401008",
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

# MSLP: deep blue (low) → white → deep red (high)
_LEVELS_MSLP = [950, 960, 970, 980, 990, 1000, 1005, 1010, 1015, 1020, 1025, 1030]
_COLORS_MSLP = [
    "#1A0090",
    "#0040CC",
    "#0080FF",
    "#40B8FF",
    "#C0E8FF",
    "#FFFFFF",
    "#FFE8C0",
    "#FFA040",
    "#FF5000",
    "#CC0000",
    "#800000",
]
CMAP_MSLP = ListedColormap(_COLORS_MSLP, name="mslp")
CMAP_MSLP.set_under(_COLORS_MSLP[0])
CMAP_MSLP.set_over(_COLORS_MSLP[-1])
NORM_MSLP = BoundaryNorm(_LEVELS_MSLP, CMAP_MSLP.N)

_WIND_CMAPS = {
    "850": (CMAP_850, NORM_850, _LEVELS_850),
    "200": (CMAP_200, NORM_200, _LEVELS_200),
}

_DIV_CMAPS = {
    "850": (CMAP_DIV_850, NORM_DIV_850, _LEVELS_DIV),
    "200": (CMAP_DIV_200, NORM_DIV_200, _LEVELS_DIV),
}

# ── ANOMALY (DIVERGING) COLORMAPS ───────────────────────────────────────────────
# Blue = below climatology, red = above climatology. Symmetric about zero.


def _make_diverging(levels, cmap_name="RdBu_r"):
    levels = list(levels)
    base = plt.get_cmap(cmap_name).resampled(len(levels) - 1)
    colors = [base(i) for i in range(len(levels) - 1)]
    cmap = ListedColormap(colors)
    cmap.set_under(colors[0])
    cmap.set_over(colors[-1])
    norm = BoundaryNorm(levels, cmap.N)
    return cmap, norm, levels


_WIND_ANOM_LEVELS = {
    "850": [-10, -8, -6, -4, -2, 0, 2, 4, 6, 8, 10],
    "200": [-30, -24, -18, -12, -6, 0, 6, 12, 18, 24, 30],
}
_WIND_ANOM_CMAPS = {lev: _make_diverging(_WIND_ANOM_LEVELS[lev]) for lev in ("850", "200")}

_GREEN_BROWN = LinearSegmentedColormap.from_list(
    "green_brown",
    ["#003300", "#2E8B2E", "#7FC97F", "#DDEEDD", "#EEDDC8", "#C9925A", "#8A5424", "#3F2206"],
)


def _make_green_brown(levels, reverse=False):
    levels = list(levels)
    n = len(levels) - 1
    src = _GREEN_BROWN.reversed() if reverse else _GREEN_BROWN
    colors = [src(i / (n - 1)) for i in range(n)]
    cmap = ListedColormap(colors)
    cmap.set_under(colors[0])
    cmap.set_over(colors[-1])
    norm = BoundaryNorm(levels, cmap.N)
    return cmap, norm, levels


_DIV_ANOM_LEVELS = [-4, -3, -2, -1, 0, 1, 2, 3, 4]
_DIV_ANOM_CMAPS = {
    "850": _make_green_brown(_DIV_ANOM_LEVELS, reverse=False),
    "200": _make_green_brown(_DIV_ANOM_LEVELS, reverse=True),
}

_MSLP_ANOM_LEVELS = [-16, -12, -8, -4, 0, 4, 8, 12, 16]
CMAP_MSLP_ANOM, NORM_MSLP_ANOM, _ = _make_diverging(_MSLP_ANOM_LEVELS)

# ── DATA LOADING ───────────────────────────────────────────────────────────────


def _normalize_ds(ds: xr.Dataset) -> xr.Dataset:
    rename = {}
    for old, new in (
        ("prediction_timedelta", "step"),
        ("latitude", "lat"),
        ("longitude", "lon"),
        ("ensemble", "number"),
        ("sample", "number"),
    ):
        if old in ds.dims or old in ds.coords:
            if new not in ds.dims and new not in ds.coords:
                rename[old] = new
    if rename:
        ds = ds.rename(rename)

    if "step" not in ds.dims and "time" in ds.dims and ds.sizes["time"] > 1:
        ds = ds.rename({"time": "step"})
    if "time" in ds.dims and ds.sizes["time"] == 1:
        ds = ds.squeeze("time", drop=True)
    if "batch" in ds.dims and ds.sizes["batch"] == 1:
        ds = ds.squeeze("batch", drop=True)

    wind_vars = (
        ("u_850", "u", "u_component_of_wind", 850),
        ("v_850", "v", "v_component_of_wind", 850),
        ("u_200", "u", "u_component_of_wind", 200),
        ("v_200", "v", "v_component_of_wind", 200),
    )
    for output_var, short_var, long_var, level in wind_vars:
        if output_var in ds:
            continue
        source_var = short_var if short_var in ds else long_var
        if source_var in ds and "level" in ds[source_var].dims:
            ds[output_var] = ds[source_var].sel(level=level)
    return ds


def _open_ds(path: Path) -> xr.Dataset:
    if path.suffix == ".zarr":
        ds = xr.open_zarr(path, chunks={})
    else:
        ds = xr.open_dataset(path, chunks={})
    return _normalize_ds(ds)


def _subset_ds(
    ds: xr.Dataset, lon_min: float, lon_max: float, lat_min: float, lat_max: float
) -> xr.Dataset:
    if lon_min < 0 and float(ds.lon.min()) >= 0:
        ds = ds.assign_coords(lon=((ds.lon + 180) % 360) - 180)
    ds = ds.sortby(["lat", "lon"])
    return ds.sel(lat=slice(lat_min, lat_max), lon=slice(lon_min, lon_max))


def _load_ds(
    path: Path, lon_min: float, lon_max: float, lat_min: float, lat_max: float
) -> xr.Dataset:
    return _subset_ds(_open_ds(path), lon_min, lon_max, lat_min, lat_max)


def _steps_per_day(ds: xr.Dataset) -> int:
    if "step" not in ds.coords or ds.sizes.get("step", 0) < 2:
        return DEFAULT_STEPS_PER_DAY
    values = ds["step"].values
    if np.issubdtype(values.dtype, np.datetime64):
        hours = np.median(np.diff(values) / np.timedelta64(1, "h"))
    elif np.issubdtype(values.dtype, np.timedelta64):
        hours = np.median(np.diff(values) / np.timedelta64(1, "h"))
    else:
        hours = np.median(np.diff(values).astype(float))
    if hours <= 0:
        return DEFAULT_STEPS_PER_DAY
    return max(1, round(24 / float(hours)))


def _step_slice(ds: xr.Dataset, day_start: int, day_end: int) -> slice:
    steps_per_day = _steps_per_day(ds)
    return slice(day_start * steps_per_day, (day_end + 1) * steps_per_day)


def _weekly_wind(ds, u_var, v_var, day_start, day_end):
    sl = _step_slice(ds, day_start, day_end)
    u = ds[u_var].isel(step=sl)
    v = ds[v_var].isel(step=sl)
    if "number" in u.dims:
        u, v = u.mean("number"), v.mean("number")
    u_m = u.mean("step").transpose("lat", "lon").values.astype(float)
    v_m = v.mean("step").transpose("lat", "lon").values.astype(float)
    return u_m, v_m, np.sqrt(u_m**2 + v_m**2)


def _weekly_mslp(ds, day_start, day_end):
    sl = _step_slice(ds, day_start, day_end)
    mslp = ds["msl"].isel(step=sl)
    if "number" in mslp.dims:
        mslp = mslp.mean("number")
    arr = mslp.mean("step").transpose("lat", "lon").values.astype(float)
    if arr.mean() > 10000:
        arr /= 100.0
    return arr


def _divergence(u, v, lat, lon, smooth_sigma=0.0):
    if smooth_sigma > 0:
        u = gaussian_filter(u, sigma=smooth_sigma)
        v = gaussian_filter(v, sigma=smooth_sigma)
    R = 6_371_000.0
    cos_lat = np.cos(np.radians(lat))[:, np.newaxis]
    du_dlam = np.gradient(u, np.radians(lon), axis=1)
    dv_dphi = np.gradient(v, np.radians(lat), axis=0)
    return (du_dlam / (R * cos_lat) + dv_dphi / R) * 1e5


# ── CLIMATOLOGY (ANOMALY BASELINE) ──────────────────────────────────────────────


def _load_clim(path: Path) -> xr.Dataset:
    """Open the day-of-year climatology written by build_climatology.py."""
    return xr.open_zarr(path)


def _align_clim(clim: xr.Dataset, ref: xr.Dataset) -> xr.Dataset:
    """Put the (already bbox-subset) climatology on the forecast grid exactly."""
    return clim.reindex(lat=ref.lat, lon=ref.lon, method="nearest", tolerance=0.2)


def _week_doys(date_str: str, day_start: int, day_end: int) -> list[int]:
    """Day-of-year values for the forecast valid period (init + day_start..day_end)."""
    y, mo, d, h = (
        int(date_str[:4]),
        int(date_str[4:6]),
        int(date_str[6:8]),
        int(date_str[9:11]),
    )
    init = datetime(y, mo, d, h)
    return [
        (init + timedelta(days=k)).timetuple().tm_yday
        for k in range(day_start, day_end + 1)
    ]


def _weekly_clim_wind(clim, u_var, v_var, doys):
    """Climatology weekly-mean wind, mirroring _weekly_wind's output."""
    uc = clim[u_var].sel(dayofyear=doys).mean("dayofyear")
    vc = clim[v_var].sel(dayofyear=doys).mean("dayofyear")
    uc = uc.transpose("lat", "lon").values.astype(float)
    vc = vc.transpose("lat", "lon").values.astype(float)
    return uc, vc, np.sqrt(uc**2 + vc**2)


def _weekly_clim_mslp(clim, doys):
    """Climatology weekly-mean MSLP (already hPa), mirroring _weekly_mslp."""
    return (
        clim["msl"].sel(dayofyear=doys).mean("dayofyear").transpose("lat", "lon").values.astype(float)
    )


# ── PLOT HELPERS ───────────────────────────────────────────────────────────────


def _valid_period_str(date_str, day_start, day_end):
    y, mo, d, h = (
        int(date_str[:4]),
        int(date_str[4:6]),
        int(date_str[6:8]),
        int(date_str[9:11]),
    )
    init = datetime(y, mo, d, h)
    t0 = init + timedelta(days=day_start)
    t1 = init + timedelta(days=day_end)
    return f"{t0.strftime('%-d %b')} – {t1.strftime('%-d %b')} {t1.year}"


_ETHIOPIA_GEOM = None


def _ethiopia_geom():
    """Natural Earth admin-0 geometry for Ethiopia (cached); None if unavailable."""
    global _ETHIOPIA_GEOM
    if _ETHIOPIA_GEOM is None:
        try:
            shp = shpreader.natural_earth(
                resolution="10m", category="cultural", name="admin_0_countries"
            )
            for rec in shpreader.Reader(shp).records():
                attrs = rec.attributes
                if "Ethiopia" in (attrs.get("NAME"), attrs.get("ADMIN"), attrs.get("SOVEREIGNT")):
                    _ETHIOPIA_GEOM = rec.geometry
                    break
        except Exception as exc:
            log.warning(f"Could not load Ethiopia outline: {exc}")
            _ETHIOPIA_GEOM = False
    return _ETHIOPIA_GEOM or None


def _decorate_ax(ax, lon_min, lon_max, lat_min, lat_max):
    ax.set_extent([lon_min, lon_max, lat_min, lat_max], crs=ccrs.PlateCarree())
    ax.coastlines(resolution="10m", linewidth=0.8)
    ax.add_feature(cfeature.BORDERS, linewidth=0.4, edgecolor="gray")
    ax.gridlines(draw_labels=False, linewidth=0.3, color="gray", linestyle="--")
    geom = _ethiopia_geom()
    if geom is not None:
        ax.add_geometries(
            [geom],
            crs=ccrs.PlateCarree(),
            facecolor="none",
            edgecolor="black",
            linewidth=1.6,
            zorder=6,
        )


def _label_panels(axes, row, col, model_label, title):
    ax = axes[row, col]
    if row == 0:
        ax.set_title(title, fontsize=10, pad=4)
    if col == 0:
        ax.text(
            -0.08,
            0.5,
            model_label,
            transform=ax.transAxes,
            fontsize=10,
            va="center",
            ha="right",
            rotation=90,
        )


def _base_fig():
    return plt.subplots(
        2,
        3,
        figsize=(14, 8),
        subplot_kw={"projection": ccrs.PlateCarree()},
        gridspec_kw={"hspace": 0.08, "wspace": 0.04},
    )


# ── FIGURE FUNCTIONS ───────────────────────────────────────────────────────────


def _wind_figure(
    aifs_full_ds,
    ens_full_ds,
    clim,
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
    cmap, norm, levels = _WIND_ANOM_CMAPS[level]
    qscale = levels[-1] * domain["aqs"]

    aifs_ds = _subset_ds(aifs_full_ds, lon_min, lon_max, lat_min, lat_max)
    ens_ds = _subset_ds(ens_full_ds, lon_min, lon_max, lat_min, lat_max)
    clim_d = _subset_ds(clim, lon_min, lon_max, lat_min, lat_max)

    fig, axes = _base_fig()
    im = None
    for row, (label, ds) in enumerate(zip(model_labels, (aifs_ds, ens_ds))):
        clim_g = _align_clim(clim_d, ds)
        lon2d, lat2d = np.meshgrid(ds.lon.values, ds.lat.values)
        for col, (wlabel, (d0, d1)) in enumerate(WEEKS.items()):
            ax = axes[row, col]
            doys = _week_doys(date_str, d0, d1)
            u, v, ws = _weekly_wind(ds, u_var, v_var, d0, d1)
            uc, vc, wsc = _weekly_clim_wind(clim_g, u_var, v_var, doys)
            ws_anom = ws - wsc
            du, dv = u - uc, v - vc
            im = ax.pcolormesh(
                lon2d,
                lat2d,
                ws_anom,
                cmap=cmap,
                norm=norm,
                shading="auto",
                transform=ccrs.PlateCarree(),
            )
            sl = slice(None, None, stride)
            ax.quiver(
                lon2d[sl, sl],
                lat2d[sl, sl],
                du[sl, sl],
                dv[sl, sl],
                transform=ccrs.PlateCarree(),
                scale=qscale,
                width=0.003,
                headwidth=3,
                color="black",
                alpha=0.8,
            )
            _decorate_ax(ax, lon_min, lon_max, lat_min, lat_max)
            _label_panels(
                axes,
                row,
                col,
                label,
                f"{wlabel}\n{_valid_period_str(date_str, d0, d1)}",
            )

    cbar_ax = fig.add_axes([0.92, 0.15, 0.015, 0.7])
    fig.colorbar(im, cax=cbar_ax, extend="both", ticks=levels).set_label(
        f"{level} hPa wind speed anomaly (m/s)", fontsize=10
    )
    fig.suptitle(
        f"{domain['label']} — {level} hPa Wind Speed Anomaly vs ERA5 clim  |  Init: {date_str}",
        fontsize=13,
        y=1.02,
    )
    fig.savefig(save_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Saved: {save_path}")


def _divcon_figure(
    aifs_full_ds,
    ens_full_ds,
    clim,
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
    qscale = _WIND_ANOM_LEVELS[level][-1] * domain["aqs"]

    aifs_ds = _subset_ds(aifs_full_ds, lon_min, lon_max, lat_min, lat_max)
    ens_ds = _subset_ds(ens_full_ds, lon_min, lon_max, lat_min, lat_max)
    clim_d = _subset_ds(clim, lon_min, lon_max, lat_min, lat_max)

    cmap_div, norm_div, levels_div = _DIV_ANOM_CMAPS[level]

    fig, axes = _base_fig()
    im = None
    for row, (label, ds) in enumerate(zip(model_labels, (aifs_ds, ens_ds))):
        clim_g = _align_clim(clim_d, ds)
        lats, lons = ds.lat.values, ds.lon.values
        lon2d, lat2d = np.meshgrid(lons, lats)
        for col, (wlabel, (d0, d1)) in enumerate(WEEKS.items()):
            ax = axes[row, col]
            doys = _week_doys(date_str, d0, d1)
            u, v, _ = _weekly_wind(ds, u_var, v_var, d0, d1)
            uc, vc, _ = _weekly_clim_wind(clim_g, u_var, v_var, doys)
            div_anom = _divergence(u, v, lats, lons, smooth_sigma) - _divergence(
                uc, vc, lats, lons, smooth_sigma
            )
            du, dv = u - uc, v - vc
            im = ax.pcolormesh(
                lon2d,
                lat2d,
                div_anom,
                cmap=cmap_div,
                norm=norm_div,
                shading="auto",
                transform=ccrs.PlateCarree(),
            )
            sl = slice(None, None, stride)
            ax.quiver(
                lon2d[sl, sl],
                lat2d[sl, sl],
                du[sl, sl],
                dv[sl, sl],
                transform=ccrs.PlateCarree(),
                scale=qscale,
                width=0.003,
                headwidth=3,
                color="black",
                alpha=0.7,
            )
            _decorate_ax(ax, lon_min, lon_max, lat_min, lat_max)
            _label_panels(
                axes,
                row,
                col,
                label,
                f"{wlabel}\n{_valid_period_str(date_str, d0, d1)}",
            )

    color_key = (
        "green = convergence  |  brown = divergence"
        if level == "850"
        else "green = divergence  |  brown = convergence"
    )
    cbar_ax = fig.add_axes([0.92, 0.15, 0.015, 0.7])
    fig.colorbar(im, cax=cbar_ax, extend="both", ticks=levels_div).set_label(
        f"{level} hPa divergence anomaly (×10⁻⁵ s⁻¹)\n{color_key}",
        fontsize=9,
    )
    fig.suptitle(
        f"{domain['label']} — {level} hPa Divergence Anomaly vs ERA5 clim  |  Init: {date_str}",
        fontsize=13,
        y=1.02,
    )
    fig.savefig(save_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Saved: {save_path}")


def _mslp_figure(
    aifs_full_ds,
    ens_full_ds,
    clim,
    date_str,
    save_path,
    domain,
    model_labels,
):
    lon_min, lon_max = domain["lon"]
    lat_min, lat_max = domain["lat"]
    stride = domain["stride"]
    qscale = _WIND_ANOM_LEVELS["850"][-1] * domain["aqs"]

    aifs_ds = _subset_ds(aifs_full_ds, lon_min, lon_max, lat_min, lat_max)
    ens_ds = _subset_ds(ens_full_ds, lon_min, lon_max, lat_min, lat_max)
    clim_d = _subset_ds(clim, lon_min, lon_max, lat_min, lat_max)
    if "msl" not in aifs_ds or "msl" not in ens_ds:
        log.info("Skipping MSLP plot: one or both models do not provide msl")
        return

    fig, axes = _base_fig()
    im = None
    for row, (label, ds) in enumerate(zip(model_labels, (aifs_ds, ens_ds))):
        clim_g = _align_clim(clim_d, ds)
        lon2d, lat2d = np.meshgrid(ds.lon.values, ds.lat.values)
        for col, (wlabel, (d0, d1)) in enumerate(WEEKS.items()):
            ax = axes[row, col]
            doys = _week_doys(date_str, d0, d1)
            u, v, _ = _weekly_wind(ds, "u_850", "v_850", d0, d1)
            uc, vc, _ = _weekly_clim_wind(clim_g, "u_850", "v_850", doys)
            du, dv = u - uc, v - vc
            mslp = _weekly_mslp(ds, d0, d1)
            mslp_anom = mslp - _weekly_clim_mslp(clim_g, doys)
            im = ax.pcolormesh(
                lon2d,
                lat2d,
                mslp_anom,
                cmap=CMAP_MSLP_ANOM,
                norm=NORM_MSLP_ANOM,
                shading="auto",
                transform=ccrs.PlateCarree(),
            )
            # Absolute isobars (context) over the anomaly shading.
            ax.contour(
                lon2d,
                lat2d,
                mslp,
                levels=np.arange(950, 1036, 4),
                colors="black",
                linewidths=0.4,
                transform=ccrs.PlateCarree(),
            )
            sl = slice(None, None, stride)
            ax.quiver(
                lon2d[sl, sl],
                lat2d[sl, sl],
                du[sl, sl],
                dv[sl, sl],
                transform=ccrs.PlateCarree(),
                scale=qscale,
                width=0.003,
                headwidth=3,
                color="black",
                alpha=0.7,
            )
            _decorate_ax(ax, lon_min, lon_max, lat_min, lat_max)
            _label_panels(
                axes,
                row,
                col,
                label,
                f"{wlabel}\n{_valid_period_str(date_str, d0, d1)}",
            )

    cbar_ax = fig.add_axes([0.92, 0.15, 0.015, 0.7])
    fig.colorbar(im, cax=cbar_ax, extend="both", ticks=_MSLP_ANOM_LEVELS).set_label(
        "MSLP anomaly (hPa)", fontsize=10
    )
    fig.suptitle(
        f"{domain['label']} — MSLP Anomaly + absolute isobars & 850 hPa anomaly wind  |  Init: {date_str}",
        fontsize=13,
        y=1.02,
    )
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
    clim_path: Path | None = None,
    deterministic_input: Path | None = None,
    ensemble_input: Path | None = None,
):
    """
    Called by model_diagnostics/utils/main.py.

    Produces wind and convergence/divergence *anomaly* figures (forecast minus
    ERA5 climatology) for both the Ethiopia and Africa domains. All output goes
    into the same ethiopia/<date>/ folder; Africa figures get an ``africa_``
    filename prefix.

    Parameters
    ----------
    base   : repo root (monsoon-onset/), passed in from main.py
    date   : YYYYMMDDTHH init date string
    deterministic_model / ensemble_model : exact configured model names.
    smooth : Gaussian sigma applied to u,v before divergence computation
    clim_path : climatology zarr from build_climatology.py
                (default: base/climatology/<CLIM_FILENAME>)
    deterministic_input / ensemble_input : optional explicit full-field paths,
                overriding the default _full_field_path() layout (handy locally).
    """
    aifs_nc = (
        Path(deterministic_input)
        if deterministic_input
        else _full_field_path(base, deterministic_model, date)
    )
    ens_nc = (
        Path(ensemble_input)
        if ensemble_input
        else _full_field_path(base, ensemble_model, date)
    )
    clim_file = Path(clim_path) if clim_path else base / "climatology" / CLIM_FILENAME
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

    for p in (aifs_nc, ens_nc):
        if not p.exists():
            log.warning(f"Skipping {date}: {p} not found")
            return
    if not clim_file.exists():
        log.warning(f"Skipping {date}: climatology {clim_file} not found")
        return

    aifs_ds = _open_ds(aifs_nc)
    ens_ds = _open_ds(ens_nc)
    clim = _load_clim(clim_file)

    try:
        domain_names = (
            ("africa",)
            if ensemble_model.lower() == "neuralgcm"
            else ("ethiopia", "africa")
        )
        for domain_name in domain_names:
            dom = DOMAINS[domain_name]
            prefix = f"{domain_name}_" if domain_name != "ethiopia" else ""
            for level, u_var, v_var in WIND_LEVELS:
                _wind_figure(
                    aifs_ds,
                    ens_ds,
                    clim,
                    level,
                    u_var,
                    v_var,
                    date,
                    out_dir / f"{prefix}wind_{level}_anom_{date}.png",
                    dom,
                    (deterministic_model, f"{ensemble_model} (mean)"),
                )
                _divcon_figure(
                    aifs_ds,
                    ens_ds,
                    clim,
                    level,
                    u_var,
                    v_var,
                    date,
                    out_dir / f"{prefix}divcon_{level}_anom_{date}.png",
                    dom,
                    (deterministic_model, f"{ensemble_model} (mean)"),
                    smooth,
                )

        _mslp_figure(
            aifs_ds,
            ens_ds,
            clim,
            date,
            out_dir / f"africa_mslp_anom_{date}.png",
            DOMAINS["mslp_extended"],
            (deterministic_model, f"{ensemble_model} (mean)"),
        )
    finally:
        aifs_ds.close()
        ens_ds.close()
        clim.close()


def _full_field_path(base: Path, model: str, date: str) -> Path:
    if model == "NeuralGCM":
        return base / "NeuralGCM" / "output" / "raw" / f"{date}.zarr"
    if model == "gencast":
        return base / "gencast" / "raw" / "output" / f"init_{date}.zarr"

    model_dir = base / "AIFS" / "output" / "raw" / model
    zarr_path = model_dir / f"init_{date}.zarr"
    if zarr_path.exists():
        return zarr_path
    return model_dir / f"init_{date}.nc"
