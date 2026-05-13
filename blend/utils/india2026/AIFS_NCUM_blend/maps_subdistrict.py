"""
maps_subdistrict.py – Monsoon onset probability maps at subdistrict level.

Usage
-----
    python maps_subdistrict.py --run 20260501T00

Outputs
-------
blend/maps_subdistrict/india2026/AIFS_NCUM_blend/
    prob_weeks1-4_<date>.pdf / .png
    map_maxperiod_<date>.pdf  / .png
"""

import argparse
from datetime import timedelta
import logging
from pathlib import Path
import logging

import geopandas as gpd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Patch
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).resolve().parent.parent.parent.parent.parent  # monsoon-onset/
OUTPUT_ROOT     = BASE_DIR / "blend" / "output" / "india2026" / "AIFS_NCUM_blend"
SHAPEFILE_DIR   = BASE_DIR / "blend" / "data" / "india2026" / "AIFS_NCUM_blend" / "data" / "india_shapefile"
SUBDISTRICT_SHP = SHAPEFILE_DIR / "Sub_districts_India_ESRI.shp"
STATE_SHP       = SHAPEFILE_DIR / "STATE_BOUNDARY.shp"
SUPPORT_DIR     = BASE_DIR / "blend" / "data" / "india2026" / "AIFS_NCUM_blend" / "data" / "support"
DISSEM_FILE     = SUPPORT_DIR / "dissemination_subdistricts.csv"
EXCLUDE_FILE    = SUPPORT_DIR / "subdistricts_to_exclude.csv"


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


def load_active_ids() -> set:
    dissem   = pd.read_csv(DISSEM_FILE)["id"].astype(str)
    excluded = pd.read_csv(EXCLUDE_FILE)["id"].astype(str)
    return set(dissem) - set(excluded)


def load_shapefiles() -> tuple:
    subdistrict_gdf = gpd.read_file(SUBDISTRICT_SHP)
    subdistrict_gdf["id"] = subdistrict_gdf["id"].astype(str)
    state_gdf = gpd.read_file(STATE_SHP)
    subdistrict_gdf = subdistrict_gdf.to_crs("EPSG:4326")
    state_gdf       = state_gdf.to_crs("EPSG:4326")
    return subdistrict_gdf, state_gdf


def max_period(vf: list) -> str:
    w1, w2, w3, w4, lat = vf
    if w1 >= 0.65:
        return 'just_week1'
    if lat >= 0.65:
        return 'later'
    sums = [w1 + w2, w2 + w3, w3 + w4, w4 + lat]
    keys = ['weeks12', 'weeks23', 'weeks34', 'weeks4later']
    return keys[int(np.argmax(sums))]


def build_color_schemes() -> tuple:
    period_order  = ['just_week1', 'weeks12', 'weeks23', 'weeks34', 'weeks4later', 'later']
    plasma        = plt.get_cmap('plasma')
    stops         = np.linspace(0.2, 1.0, len(period_order))
    period_colors = {k: plasma(s) for k, s in zip(period_order, stops)}
    period_colors['none'] = '#d3d3d3'

    prob_bins = [0, 0.1, 0.2, 0.3, 0.4, 1.0]
    prob_cmap = ListedColormap(plt.get_cmap('plasma_r')(np.linspace(0, 1, len(prob_bins) - 1)))
    prob_norm = BoundaryNorm(prob_bins, ncolors=len(prob_bins) - 1, clip=True)

    return period_order, period_colors, prob_bins, prob_cmap, prob_norm


def save_fig(out_dir, fig, stem: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / f"{stem}.png", dpi=150, bbox_inches='tight')
    fig.savefig(out_dir / f"{stem}.pdf",            bbox_inches='tight')


def plot_weekly_probs(grp, t, merged_gdf, state_gdf, active_ids,
                      prob_cmap, prob_norm, prob_bins, out_dir):
    ds = t.strftime('%Y-%m-%d')
    week_titles = {
        i: (
            f"{(t + timedelta(days=(i-1)*7+1)).strftime('%m/%d/%Y')} – "
            f"{(t + timedelta(days=(i-1)*7+7)).strftime('%m/%d/%Y')}"
        )
        for i in range(1, 5)
    }

    forecast_ids = set(grp["id"])
    on_ids = active_ids & forecast_ids

    fig, axes = plt.subplots(1, 4, figsize=(20, 6),
                             sharex=True, sharey=True,
                             gridspec_kw={'wspace': 0.03})
    week_cols = ['week1', 'week2', 'week3', 'week4']

    for i, (ax, wcol) in enumerate(zip(axes, week_cols), 1):
        off_gdf = merged_gdf[~merged_gdf["id"].isin(on_ids)]
        if not off_gdf.empty:
            off_gdf.plot(ax=ax, color='#d3d3d3', linewidth=0.0)

        active_gdf = merged_gdf[merged_gdf["id"].isin(on_ids)].copy()
        active_gdf = active_gdf.merge(grp[["id", wcol]], on="id", how="left")
        active_gdf = active_gdf.dropna(subset=[wcol])

        if not active_gdf.empty:
            active_gdf["color"] = active_gdf[wcol].apply(
                lambda v: prob_cmap(prob_norm(v))
            )
            active_gdf.plot(ax=ax, color=active_gdf["color"].tolist(),
                            linewidth=0.1, edgecolor='none')

        merged_gdf.boundary.plot(ax=ax, linewidth=0.1, edgecolor='#888888')
        state_gdf.boundary.plot(ax=ax, linewidth=1.2, edgecolor='black')

        ax.set_title(week_titles[i], fontsize=10, pad=6)
        ax.set_xlabel('Longitude')
        if i == 1:
            ax.set_ylabel('Latitude')
        else:
            ax.set_ylabel('')

    sm = plt.cm.ScalarMappable(norm=prob_norm, cmap=prob_cmap)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=list(axes), orientation='horizontal',
                        fraction=0.04, pad=0.08)
    cbar.set_label('Probability of onset')

    fig.suptitle(f"Monsoon onset probability by week – forecast date {ds}", fontsize=12)
    fig.subplots_adjust(left=0.04, right=0.96, top=0.88, bottom=0.18)
    save_fig(out_dir, fig, f"prob_weeks1-4_{ds}")
    plt.close(fig)


def plot_max_period(grp, t, merged_gdf, state_gdf, active_ids,
                    period_order, period_colors, pdays, out_dir):
    ds = t.strftime('%Y-%m-%d')

    forecast_ids = set(grp["id"])
    on_ids = active_ids & forecast_ids

    fig, ax = plt.subplots(figsize=(7, 8))

    off_gdf = merged_gdf[~merged_gdf["id"].isin(on_ids)]
    if not off_gdf.empty:
        off_gdf.plot(ax=ax, color=period_colors['none'], linewidth=0.0)

    active_gdf = merged_gdf[merged_gdf["id"].isin(on_ids)].copy()
    prob_cols  = ['week1', 'week2', 'week3', 'week4', 'later']
    active_gdf = active_gdf.merge(grp[["id"] + prob_cols], on="id", how="left")
    active_gdf = active_gdf.dropna(subset=prob_cols)

    if not active_gdf.empty:
        def row_period(r):
            vf = [r['week1'], r['week2'], r['week3'], r['week4'], r['later']]
            return max_period(vf)

        active_gdf["period"] = active_gdf.apply(row_period, axis=1)
        active_gdf["color"]  = active_gdf["period"].map(period_colors)
        active_gdf.plot(ax=ax, color=active_gdf["color"].tolist(),
                        linewidth=0.1, edgecolor='none')

    merged_gdf.boundary.plot(ax=ax, linewidth=0.1, edgecolor='#888888')
    state_gdf.boundary.plot(ax=ax, linewidth=1.5, edgecolor='black')

    def period_label(k):
        d1, d2 = pdays[k]
        start  = (t + timedelta(days=d1)).strftime('%m/%d/%Y')
        if d2 is None:
            return f"{start}+"
        end = (t + timedelta(days=d2)).strftime('%m/%d/%Y')
        return f"{start} – {end}"

    handles = [
        Patch(facecolor=period_colors[k], edgecolor='#555', linewidth=0.5,
              label=period_label(k))
        for k in period_order
    ]
    handles.append(Patch(facecolor=period_colors['none'], edgecolor='#555',
                         linewidth=0.5, label='Onset already declared'))

    ax.legend(handles=handles, title='Period with highest onset probability',
              loc='lower left', fontsize=8, title_fontsize=8, ncol=2)
    ax.set_xlabel('Longitude')
    ax.set_ylabel('Latitude')
    ax.set_title(f"Peak monsoon onset period – forecast date {ds}", fontsize=12)

    plt.tight_layout()
    save_fig(out_dir, fig, f"map_maxperiod_{ds}")
    plt.close(fig)


def make_maps(out_dir):


    summary    = load_data(out_dir)
    active_ids = load_active_ids()

    logging.info("Loading shapefiles ...")
    subdistrict_gdf, state_gdf = load_shapefiles()
    merged_gdf = subdistrict_gdf[subdistrict_gdf["id"].isin(active_ids)].copy()

    period_order, period_colors, prob_bins, prob_cmap, prob_norm = build_color_schemes()

    pdays = {
        'just_week1':   (1, 7),
        'weeks12':      (1, 14),
        'weeks23':      (8, 21),
        'weeks34':      (15, 28),
        'weeks4later':  (22, None),
        'later':        (29, None),
    }

    for t, grp in summary.groupby('time'):
        ds = t.strftime('%Y-%m-%d')
        logging.info(f"Plotting {ds}...")

        plot_weekly_probs(grp, t, merged_gdf, state_gdf, active_ids,
                          prob_cmap, prob_norm, prob_bins, out_dir)
        plot_max_period(grp, t, merged_gdf, state_gdf, active_ids,
                        period_order, period_colors, pdays, out_dir)

    logging.info(f"Done. Maps saved to {out_dir}/")

