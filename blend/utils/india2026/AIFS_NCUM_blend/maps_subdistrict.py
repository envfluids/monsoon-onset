"""
maps_subdistrict.py – Monsoon onset probability maps at subdistrict level.

Usage
-----
    python maps_subdistrict.py --run 20260501T00

Outputs
-------
blend/maps_subdistrict/india2026/AIFS_NCUM_blend/
    prob_weeks1-4_<date>.pdf / .png          - 4-panel weekly probability map
    map_maxperiod_<date>.pdf  / .png         - single map coloured by peak onset period
    clim_prob_weeks1-4_<date>.pdf / .png     - 4-panel climatological probability map
    anom_weeks1-4_<date>.pdf / .png          - 4-panel probability anomaly map
    rain_<model>_weeks1-4_<date>.pdf / .png  - 4-panel daily rainfall map per model

Notes
-----
prob_clim_mr_week* columns are stored on the logit (log-odds) scale.  The script
applies a sigmoid transform to convert them to probabilities before plotting so
that the climatological maps and anomaly maps share the same [0,1] probability
space as the week1-week4 columns.

Requirements
------------
geopandas, matplotlib, pandas, numpy  (all in the project venv)
"""

import logging
import re
from datetime import timedelta
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Patch

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent  # monsoon-onset/
OUTPUT_ROOT = BASE_DIR / "blend" / "output" / "india2026" / "AIFS_NCUM_blend"
SHAPEFILE_DIR = (
    BASE_DIR
    / "blend"
    / "data"
    / "india2026"
    / "AIFS_NCUM_blend"
    / "data"
    / "india_shapefile"
)
SUBDISTRICT_SHP = SHAPEFILE_DIR / "Sub_districts_India_ESRI.shp"
STATE_SHP = SHAPEFILE_DIR / "STATE_BOUNDARY.shp"
SUPPORT_DIR = (
    BASE_DIR / "blend" / "data" / "india2026" / "AIFS_NCUM_blend" / "data" / "support"
)
DISSEM_FILE = SUPPORT_DIR / "dissemination_subdistricts.csv"
EXCLUDE_FILE = SUPPORT_DIR / "subdistricts_to_exclude.csv"


def find_latest_run() -> Path:
    runs = sorted(
        [p for p in OUTPUT_ROOT.iterdir() if p.is_dir()],
        key=lambda p: p.name,
    )
    if not runs:
        raise FileNotFoundError(f"No run folders found in {OUTPUT_ROOT}")
    return runs[-1]


def load_data(run_dir: Path) -> pd.DataFrame:
    csv = run_dir / "blend_output_summary.csv"
    if not csv.exists():
        raise FileNotFoundError(f"blend_output_summary.csv not found in {run_dir}")
    df = pd.read_csv(csv, parse_dates=["time"])
    df["id"] = df["id"].astype(str)
    return df


def load_full_data(run_dir: Path) -> pd.DataFrame:
    csv = run_dir / "blend_output_full.csv"
    if not csv.exists():
        raise FileNotFoundError(f"blend_output_full.csv not found in {run_dir}")
    df = pd.read_csv(csv, parse_dates=["time"])
    df["id"] = df["id"].astype(str)
    return df


def detect_forecast_names(df: pd.DataFrame) -> list:
    """Return sorted list of model names found as <name>_rain_daily_week1 columns."""
    names = sorted(
        {
            m.group(1)
            for col in df.columns
            for m in [re.match(r"^(.+)_rain_daily_week1$", col)]
            if m
        }
    )
    return names


def load_active_ids() -> set:
    dissem = pd.read_csv(DISSEM_FILE)["id"].astype(str)
    excluded = pd.read_csv(EXCLUDE_FILE)["id"].astype(str)
    return set(dissem) - set(excluded)


def load_shapefiles() -> tuple:
    subdistrict_gdf = gpd.read_file(SUBDISTRICT_SHP)
    subdistrict_gdf["id"] = subdistrict_gdf["id"].astype(str)
    state_gdf = gpd.read_file(STATE_SHP)
    subdistrict_gdf = subdistrict_gdf.to_crs("EPSG:4326")
    state_gdf = state_gdf.to_crs("EPSG:4326")
    return subdistrict_gdf, state_gdf


def max_period(vf: list) -> str:
    """Return the period label with the highest mass (same rule as old maps.py)."""
    w1, w2, w3, w4, lat = vf
    if w1 >= 0.65:
        return "just_week1"
    if lat >= 0.65:
        return "later"
    sums = [w1 + w2, w2 + w3, w3 + w4, w4 + lat]
    keys = ["weeks12", "weeks23", "weeks34", "weeks4later"]
    return keys[int(np.argmax(sums))]


def build_color_schemes() -> tuple:
    period_order = [
        "just_week1",
        "weeks12",
        "weeks23",
        "weeks34",
        "weeks4later",
        "later",
    ]
    plasma = plt.get_cmap("plasma")
    stops = np.linspace(0.2, 1.0, len(period_order))
    period_colors = {k: plasma(s) for k, s in zip(period_order, stops)}
    period_colors["none"] = "#d3d3d3"

    prob_bins = [0, 0.1, 0.2, 0.3, 0.4, 1.0]
    prob_cmap = ListedColormap(
        plt.get_cmap("plasma_r")(np.linspace(0, 1, len(prob_bins) - 1))
    )
    prob_norm = BoundaryNorm(prob_bins, ncolors=len(prob_bins) - 1, clip=True)

    return period_order, period_colors, prob_bins, prob_cmap, prob_norm


def build_anomaly_colorscheme() -> tuple:
    """Diverging colorblind-friendly (blue/red) scale centred at 0 for prob anomalies."""
    anom_bins = [-0.40, -0.20, -0.10, -0.05, -0.02, 0.00, 0.02, 0.05, 0.10, 0.20, 0.40]
    n = len(anom_bins) - 1
    anom_cmap = ListedColormap(plt.get_cmap("RdBu")(np.linspace(0, 1, n)))
    anom_norm = BoundaryNorm(anom_bins, ncolors=n, clip=True)
    return anom_bins, anom_cmap, anom_norm


def build_rainfall_colorscheme() -> tuple:
    """Sequential yellow-green-blue scale for daily rainfall (mm/day)."""
    rain_bins = [0, 2, 5, 10, 20, 30, 50, 80]
    n = len(rain_bins) - 1
    rain_cmap = ListedColormap(plt.get_cmap("YlGnBu")(np.linspace(0.05, 1.0, n)))
    rain_norm = BoundaryNorm(rain_bins, ncolors=n, clip=True)
    return rain_bins, rain_cmap, rain_norm


def save_fig(out_dir, fig, stem: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / f"{stem}.png", dpi=150, bbox_inches="tight")
    fig.savefig(out_dir / f"{stem}.pdf", bbox_inches="tight")


def _week_titles(t: "pd.Timestamp") -> dict:
    return {
        i: (
            f"{(t + timedelta(days=(i - 1) * 7 + 1)).strftime('%m/%d/%Y')} – "
            f"{(t + timedelta(days=(i - 1) * 7 + 7)).strftime('%m/%d/%Y')}"
        )
        for i in range(1, 5)
    }


def plot_weekly_probs(
    grp, t, merged_gdf, state_gdf, active_ids, prob_cmap, prob_norm, prob_bins, out_dir
):
    """4-panel map: one panel per week (weeks 1–4)."""
    ds = t.strftime("%Y-%m-%d")
    week_titles = _week_titles(t)

    forecast_ids = set(grp["id"])
    on_ids = active_ids & forecast_ids

    fig, axes = plt.subplots(
        1, 4, figsize=(20, 6), sharex=True, sharey=True, gridspec_kw={"wspace": 0.03}
    )
    week_cols = ["week1", "week2", "week3", "week4"]

    for i, (ax, wcol) in enumerate(zip(axes, week_cols), 1):
        off_gdf = merged_gdf[~merged_gdf["id"].isin(on_ids)]
        if not off_gdf.empty:
            off_gdf.plot(ax=ax, color="#d3d3d3", linewidth=0.0)

        active_gdf = merged_gdf[merged_gdf["id"].isin(on_ids)].copy()
        active_gdf = active_gdf.merge(grp[["id", wcol]], on="id", how="left")
        active_gdf = active_gdf.dropna(subset=[wcol])

        if not active_gdf.empty:
            active_gdf["color"] = active_gdf[wcol].apply(
                lambda v: prob_cmap(prob_norm(v))
            )
            active_gdf.plot(
                ax=ax,
                color=active_gdf["color"].tolist(),
                linewidth=0.1,
                edgecolor="none",
            )

        merged_gdf.boundary.plot(ax=ax, linewidth=0.1, edgecolor="#888888")
        state_gdf.boundary.plot(ax=ax, linewidth=1.2, edgecolor="black")

        ax.set_title(week_titles[i], fontsize=10, pad=6)
        ax.set_xlabel("Longitude")
        if i == 1:
            ax.set_ylabel("Latitude")
        else:
            ax.set_ylabel("")

    sm = plt.cm.ScalarMappable(norm=prob_norm, cmap=prob_cmap)
    sm.set_array([])
    cbar = fig.colorbar(
        sm, ax=list(axes), orientation="horizontal", fraction=0.04, pad=0.08
    )
    cbar.set_label("Probability of onset")

    fig.suptitle(f"Monsoon onset probability by week – forecast date {ds}", fontsize=12)
    fig.subplots_adjust(left=0.04, right=0.96, top=0.88, bottom=0.18)
    save_fig(out_dir, fig, f"prob_weeks1-4_{ds}")
    plt.close(fig)


def plot_max_period(
    grp,
    t,
    merged_gdf,
    state_gdf,
    active_ids,
    period_order,
    period_colors,
    pdays,
    out_dir,
):
    """Single map coloured by the period with highest onset probability mass."""
    ds = t.strftime("%Y-%m-%d")

    forecast_ids = set(grp["id"])
    on_ids = active_ids & forecast_ids

    fig, ax = plt.subplots(figsize=(7, 8))

    off_gdf = merged_gdf[~merged_gdf["id"].isin(on_ids)]
    if not off_gdf.empty:
        off_gdf.plot(ax=ax, color=period_colors["none"], linewidth=0.0)

    active_gdf = merged_gdf[merged_gdf["id"].isin(on_ids)].copy()
    prob_cols = ["week1", "week2", "week3", "week4", "later"]
    active_gdf = active_gdf.merge(grp[["id"] + prob_cols], on="id", how="left")
    active_gdf = active_gdf.dropna(subset=prob_cols)

    if not active_gdf.empty:

        def row_period(r):
            vf = [r["week1"], r["week2"], r["week3"], r["week4"], r["later"]]
            return max_period(vf)

        active_gdf["period"] = active_gdf.apply(row_period, axis=1)
        active_gdf["color"] = active_gdf["period"].map(period_colors)
        active_gdf.plot(
            ax=ax, color=active_gdf["color"].tolist(), linewidth=0.1, edgecolor="none"
        )

    merged_gdf.boundary.plot(ax=ax, linewidth=0.1, edgecolor="#888888")
    state_gdf.boundary.plot(ax=ax, linewidth=1.5, edgecolor="black")

    def period_label(k):
        d1, d2 = pdays[k]
        start = (t + timedelta(days=d1)).strftime("%m/%d/%Y")
        if d2 is None:
            return f"{start}+"
        end = (t + timedelta(days=d2)).strftime("%m/%d/%Y")
        return f"{start} – {end}"

    handles = [
        Patch(
            facecolor=period_colors[k],
            edgecolor="#555",
            linewidth=0.5,
            label=period_label(k),
        )
        for k in period_order
    ]
    handles.append(
        Patch(
            facecolor=period_colors["none"],
            edgecolor="#555",
            linewidth=0.5,
            label="Onset already declared",
        )
    )

    ax.legend(
        handles=handles,
        title="Period with highest onset probability",
        loc="lower left",
        fontsize=8,
        title_fontsize=8,
        ncol=2,
    )
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(f"Peak monsoon onset period – forecast date {ds}", fontsize=12)

    plt.tight_layout()
    save_fig(out_dir, fig, f"map_maxperiod_{ds}")
    plt.close(fig)


def plot_clim_probs(
    grp, t, merged_gdf, state_gdf, active_ids, prob_cmap, prob_norm, prob_bins, out_dir
):
    """4-panel climatological onset probability map (weeks 1–4).

    prob_clim_mr_week* values are on the logit scale and are converted
    to probabilities via sigmoid before plotting.
    """
    ds = t.strftime("%Y-%m-%d")
    week_titles = _week_titles(t)

    forecast_ids = set(grp["id"])
    on_ids = active_ids & forecast_ids

    fig, axes = plt.subplots(
        1, 4, figsize=(20, 6), sharex=True, sharey=True, gridspec_kw={"wspace": 0.03}
    )

    clim_cols = [
        "prob_clim_mr_week1",
        "prob_clim_mr_week2",
        "prob_clim_mr_week3",
        "prob_clim_mr_week4",
    ]

    for i, (ax, ccol) in enumerate(zip(axes, clim_cols), 1):
        off_gdf = merged_gdf[~merged_gdf["id"].isin(on_ids)]
        if not off_gdf.empty:
            off_gdf.plot(ax=ax, color="#d3d3d3", linewidth=0.0)

        active_gdf = merged_gdf[merged_gdf["id"].isin(on_ids)].copy()
        active_gdf = active_gdf.merge(grp[["id", ccol]], on="id", how="left")
        active_gdf = active_gdf.dropna(subset=[ccol])

        if not active_gdf.empty:
            # sigmoid converts logit → probability
            active_gdf["clim_prob"] = 1.0 / (1.0 + np.exp(-active_gdf[ccol]))
            active_gdf["color"] = active_gdf["clim_prob"].apply(
                lambda v: prob_cmap(prob_norm(v))
            )
            active_gdf.plot(
                ax=ax,
                color=active_gdf["color"].tolist(),
                linewidth=0.1,
                edgecolor="none",
            )

        merged_gdf.boundary.plot(ax=ax, linewidth=0.1, edgecolor="#888888")
        state_gdf.boundary.plot(ax=ax, linewidth=1.2, edgecolor="black")

        ax.set_title(week_titles[i], fontsize=10, pad=6)
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude" if i == 1 else "")

    sm = plt.cm.ScalarMappable(norm=prob_norm, cmap=prob_cmap)
    sm.set_array([])
    cbar = fig.colorbar(
        sm, ax=list(axes), orientation="horizontal", fraction=0.04, pad=0.08
    )
    cbar.set_label("Climatological probability of onset")

    fig.suptitle(
        f"Climatological monsoon onset probability by week – forecast date {ds}",
        fontsize=12,
    )
    fig.subplots_adjust(left=0.04, right=0.96, top=0.88, bottom=0.18)
    save_fig(out_dir, fig, f"clim_prob_weeks1-4_{ds}")
    plt.close(fig)


def plot_prob_anomalies(
    grp, t, merged_gdf, state_gdf, active_ids, anom_bins, anom_cmap, anom_norm, out_dir
):
    """4-panel probability anomaly map: forecast prob minus climatological prob.

    Anomaly = week[n] - sigmoid(prob_clim_mr_week[n]).
    Blue = above climatology, red = below climatology.
    """
    ds = t.strftime("%Y-%m-%d")
    week_titles = _week_titles(t)

    forecast_ids = set(grp["id"])
    on_ids = active_ids & forecast_ids

    fig, axes = plt.subplots(
        1, 4, figsize=(20, 6), sharex=True, sharey=True, gridspec_kw={"wspace": 0.03}
    )

    week_cols = ["week1", "week2", "week3", "week4"]
    clim_cols = [
        "prob_clim_mr_week1",
        "prob_clim_mr_week2",
        "prob_clim_mr_week3",
        "prob_clim_mr_week4",
    ]

    needed = ["id"] + week_cols + clim_cols
    grp_sub = grp[[c for c in needed if c in grp.columns]]

    for i, (ax, wcol, ccol) in enumerate(zip(axes, week_cols, clim_cols), 1):
        off_gdf = merged_gdf[~merged_gdf["id"].isin(on_ids)]
        if not off_gdf.empty:
            off_gdf.plot(ax=ax, color="#d3d3d3", linewidth=0.0)

        active_gdf = merged_gdf[merged_gdf["id"].isin(on_ids)].copy()
        active_gdf = active_gdf.merge(grp_sub[["id", wcol, ccol]], on="id", how="left")
        active_gdf = active_gdf.dropna(subset=[wcol, ccol])

        if not active_gdf.empty:
            clim_prob = 1.0 / (1.0 + np.exp(-active_gdf[ccol]))
            active_gdf["anom"] = active_gdf[wcol].values - clim_prob.values
            active_gdf["color"] = active_gdf["anom"].apply(
                lambda v: anom_cmap(anom_norm(v))
            )
            active_gdf.plot(
                ax=ax,
                color=active_gdf["color"].tolist(),
                linewidth=0.1,
                edgecolor="none",
            )

        merged_gdf.boundary.plot(ax=ax, linewidth=0.1, edgecolor="#888888")
        state_gdf.boundary.plot(ax=ax, linewidth=1.2, edgecolor="black")

        ax.set_title(week_titles[i], fontsize=10, pad=6)
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude" if i == 1 else "")

    sm = plt.cm.ScalarMappable(norm=anom_norm, cmap=anom_cmap)
    sm.set_array([])
    cbar = fig.colorbar(
        sm, ax=list(axes), orientation="horizontal", fraction=0.04, pad=0.08
    )
    cbar.set_label("Probability anomaly (forecast − climatology)")

    fig.suptitle(
        f"Monsoon onset probability anomaly by week – forecast date {ds}", fontsize=12
    )
    fig.subplots_adjust(left=0.04, right=0.96, top=0.88, bottom=0.18)
    save_fig(out_dir, fig, f"anom_weeks1-4_{ds}")
    plt.close(fig)


def plot_rainfall(
    grp,
    t,
    merged_gdf,
    state_gdf,
    active_ids,
    forecast_name: str,
    rain_bins,
    rain_cmap,
    rain_norm,
    out_dir,
):
    """4-panel daily rainfall map for a single forecast model (weeks 1–4)."""
    ds = t.strftime("%Y-%m-%d")
    week_titles = _week_titles(t)

    forecast_ids = set(grp["id"])
    on_ids = active_ids & forecast_ids

    fig, axes = plt.subplots(
        1, 4, figsize=(20, 6), sharex=True, sharey=True, gridspec_kw={"wspace": 0.03}
    )

    rain_cols = [f"{forecast_name}_rain_daily_week{w}" for w in range(1, 5)]

    for i, (ax, rcol) in enumerate(zip(axes, rain_cols), 1):
        if rcol not in grp.columns:
            ax.set_visible(False)
            continue

        off_gdf = merged_gdf[~merged_gdf["id"].isin(on_ids)]
        if not off_gdf.empty:
            off_gdf.plot(ax=ax, color="#d3d3d3", linewidth=0.0)

        active_gdf = merged_gdf[merged_gdf["id"].isin(on_ids)].copy()
        active_gdf = active_gdf.merge(grp[["id", rcol]], on="id", how="left")
        active_gdf = active_gdf.dropna(subset=[rcol])

        if not active_gdf.empty:
            active_gdf["color"] = active_gdf[rcol].apply(
                lambda v: rain_cmap(rain_norm(v))
            )
            active_gdf.plot(
                ax=ax,
                color=active_gdf["color"].tolist(),
                linewidth=0.1,
                edgecolor="none",
            )

        merged_gdf.boundary.plot(ax=ax, linewidth=0.1, edgecolor="#888888")
        state_gdf.boundary.plot(ax=ax, linewidth=1.2, edgecolor="black")

        ax.set_title(week_titles[i], fontsize=10, pad=6)
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude" if i == 1 else "")

    sm = plt.cm.ScalarMappable(norm=rain_norm, cmap=rain_cmap)
    sm.set_array([])
    cbar = fig.colorbar(
        sm, ax=list(axes), orientation="horizontal", fraction=0.04, pad=0.08
    )
    cbar.set_label("Daily rainfall (mm/day)")

    fig.suptitle(
        f"Daily rainfall by week [{forecast_name.upper()}] – forecast date {ds}",
        fontsize=12,
    )
    fig.subplots_adjust(left=0.04, right=0.96, top=0.88, bottom=0.18)
    save_fig(out_dir, fig, f"rain_{forecast_name}_weeks1-4_{ds}")
    plt.close(fig)


def make_maps(out_dir: Path):

    summary = load_data(out_dir)
    full_data = load_full_data(out_dir)
    active_ids = load_active_ids()

    logging.info("Loading shapefiles ...")
    subdistrict_gdf, state_gdf = load_shapefiles()
    merged_gdf = subdistrict_gdf[subdistrict_gdf["id"].isin(active_ids)].copy()

    period_order, period_colors, prob_bins, prob_cmap, prob_norm = build_color_schemes()
    anom_bins, anom_cmap, anom_norm = build_anomaly_colorscheme()
    rain_bins, rain_cmap, rain_norm = build_rainfall_colorscheme()

    forecast_names = detect_forecast_names(full_data)
    logging.info(f"Forecast models found: {forecast_names}")

    pdays = {
        "just_week1": (1, 7),
        "weeks12": (1, 14),
        "weeks23": (8, 21),
        "weeks34": (15, 28),
        "weeks4later": (22, None),
        "later": (29, None),
    }

    full_by_time = {t: g for t, g in full_data.groupby("time")}

    for t, grp in summary.groupby("time"):
        ds = t.strftime("%Y-%m-%d")
        logging.info(f"Plotting {ds} ...")

        plot_weekly_probs(
            grp,
            t,
            merged_gdf,
            state_gdf,
            active_ids,
            prob_cmap,
            prob_norm,
            prob_bins,
            out_dir,
        )

        plot_max_period(
            grp,
            t,
            merged_gdf,
            state_gdf,
            active_ids,
            period_order,
            period_colors,
            pdays,
            out_dir,
        )

        full_grp = full_by_time.get(t)
        if full_grp is not None:
            plot_clim_probs(
                full_grp,
                t,
                merged_gdf,
                state_gdf,
                active_ids,
                prob_cmap,
                prob_norm,
                prob_bins,
                out_dir,
            )

            plot_prob_anomalies(
                full_grp,
                t,
                merged_gdf,
                state_gdf,
                active_ids,
                anom_bins,
                anom_cmap,
                anom_norm,
                out_dir,
            )

            for fcst in forecast_names:
                plot_rainfall(
                    full_grp,
                    t,
                    merged_gdf,
                    state_gdf,
                    active_ids,
                    fcst,
                    rain_bins,
                    rain_cmap,
                    rain_norm,
                    out_dir,
                )
        else:
            logging.warning(f"No full_data row for {ds}, skipping clim/anom/rain maps")

    logging.info(f"Done. Maps saved to {out_dir}/")
