from datetime import timedelta
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.colors import TwoSlopeNorm, LinearSegmentedColormap
import geopandas as gpd
from shapely.geometry import Polygon
from pathlib import Path
import logging

def make_maps(summary, date):
    base = Path(__file__).resolve().parent
    india_shapefile = base / "data" / "india_shapefile" / "India_Country_Boundary.shp"

    # ------------------------------------------------------------------------------
    # 0) Ensure output folder exists
    # ------------------------------------------------------------------------------
    output_dir = base / "output" / date / "maps"
    os.makedirs(output_dir, exist_ok=True)


    # ------------------------------------------------------------------------------
    # 1) Load data
    # ------------------------------------------------------------------------------
    # summary = pd.read_csv("blend_output_summary.csv", parse_dates=["time"])
    preds_df = summary.copy()

    # ------------------------------------------------------------------------------
    # 2) Add climatology & combined‐weeks & outlines
    # ------------------------------------------------------------------------------
    for i in range(1, 5):
        preds_df[f'Climatology_p_{i}'] = preds_df[f'clim_week{i}']
        preds_df[f'Forecast_p_{i}']     = preds_df[f'week{i}']

    preds_df['Climatology_p_12'] = preds_df['Climatology_p_1'] + preds_df['Climatology_p_2']
    preds_df['Climatology_p_34'] = preds_df['Climatology_p_3'] + preds_df['Climatology_p_4']
    preds_df['Forecast_p_12']    = preds_df['Forecast_p_1']   + preds_df['Forecast_p_2']
    preds_df['Forecast_p_34']    = preds_df['Forecast_p_3']   + preds_df['Forecast_p_4']

    for tag in ['1','2','3','4','12','34']:
        clim = preds_df[f'Climatology_p_{tag}']
        fcst = preds_df[f'Forecast_p_{tag}']
        preds_df[f'outline_{tag}'] = np.where(
            clim >= fcst + 0.10, 'red',
            np.where(fcst >= clim + 0.10, 'green', 'black')
        )

    # ------------------------------------------------------------------------------
    # 3) Rectangle corners
    # ------------------------------------------------------------------------------
    preds_df['lon_min'] = preds_df['lon'] - 1
    preds_df['lon_max'] = preds_df['lon'] + 1
    preds_df['lat_min'] = preds_df['lat'] - 1
    preds_df['lat_max'] = preds_df['lat'] + 1

    # ------------------------------------------------------------------------------
    # 4) India boundary as GeoDataFrame
    # ------------------------------------------------------------------------------
    india_gdf = gpd.read_file(india_shapefile).to_crs("EPSG:4326")

    # ------------------------------------------------------------------------------
    # 5) Color map & norm
    # ------------------------------------------------------------------------------
    cmap = LinearSegmentedColormap.from_list('rwg', ['red','white','green'])
    norm = TwoSlopeNorm(vmin=0, vcenter=0.25, vmax=1)

    # which probability fields
    plots = {
        '1':  'Forecast_p_1',
        '2':  'Forecast_p_2',
        '3':  'Forecast_p_3',
        '4':  'Forecast_p_4',
        '12': 'Forecast_p_12',
        '34': 'Forecast_p_34'
    }

    # ------------------------------------------------------------------------------
    # 6) Scalar‐value maps (weeks 1,2,3,4,12,34)
    # ------------------------------------------------------------------------------
    for t, grp in preds_df.groupby('time'):
        date_str = t.strftime("%Y-%m-%d")
        date_str_fmt = t.strftime("%m/%d/%Y")

        for tag, var in plots.items():
            # compute week‐range offsets
            if tag in ['1','2','3','4']:
                start_off = (int(tag)-1)*7 + 1
                end_off   = start_off + 6
            elif tag == '12':
                start_off, end_off = 1, 14
            else:  # '34'
                start_off, end_off = 15, 28

            start_dt = t + timedelta(days=start_off)
            end_dt   = t + timedelta(days=end_off)

            fig, ax = plt.subplots(figsize=(6, 6))
            india_gdf.boundary.plot(ax=ax, linewidth=0.5, edgecolor='black')

            for _, row in grp.iterrows():
                val = row[var]
                if pd.notna(val):
                    # colored cell
                    rect = Rectangle(
                        (row['lon_min'], row['lat_min']),
                        row['lon_max'] - row['lon_min'],
                        row['lat_max'] - row['lat_min'],
                        facecolor=cmap(norm(val)),
                        edgecolor='black',
                        linewidth=0.5
                    )
                    ax.add_patch(rect)

                    # percentage text with opaque white box
                    pct = int(round(val * 100))
                    ax.text(
                        row['lon'], row['lat'],
                        f"{pct}%",
                        ha='center', va='center',
                        fontsize=8,
                        bbox=dict(facecolor='white', edgecolor='none', pad=1)
                    )

                    # colored outline
                    ol = Rectangle(
                        (row['lon_min'], row['lat_min']),
                        row['lon_max'] - row['lon_min'],
                        row['lat_max'] - row['lat_min'],
                        fill=False,
                        edgecolor='k',
                        linewidth=1.5
                    )
                    ax.add_patch(ol)

            ax.set_title(f"{date_str_fmt} forecast: "
                        f"{start_dt.strftime('%m/%d/%Y')} - {end_dt.strftime('%m/%d/%Y')}")
            ax.set_xlim(68, 98)
            ax.set_ylim(6, 38)
            ax.set_aspect('equal')
            ax.axis('off')
            plt.tight_layout()
            logging.info(f"Saving map for {var} on {date_str}")
            plt.savefig(f"{output_dir}/map_week{tag}.png", dpi=200)
            plt.close(fig)
    # ------------------------------------------------------------------------------
    # 7) Bar‐chart maps (week1, week2, week3, week4, later) with cell outlines
    # ------------------------------------------------------------------------------
    for t, grp in preds_df.groupby('time'):
        date_str = t.strftime("%Y-%m-%d")
        date_str_fmt = t.strftime("%m/%d/%Y")

        fig, ax = plt.subplots(figsize=(6, 6))
        india_gdf.boundary.plot(ax=ax, linewidth=0.5, edgecolor='black')

        for _, row in grp.iterrows():
            # dimensions of the full cell
            w = row['lon_max'] - row['lon_min']
            h = row['lat_max'] - row['lat_min']
            n_bars = 5
            bar_w = (w / n_bars) * 0.8
            gap   = (w / n_bars) * 0.2

            # draw the little bars
            for i, col in enumerate([
                'Forecast_p_1',
                'Forecast_p_2',
                'Forecast_p_3',
                'Forecast_p_4',
                'later'
            ]):
                val = row[col]
                if pd.notna(val):
                    bar_h = h * val
                    x0 = row['lon_min'] + i * (w / n_bars) + gap/2
                    bar = Rectangle(
                        (x0, row['lat_min']),
                        bar_w, bar_h,
                        facecolor=cmap(norm(val)),
                        edgecolor='none'
                    )
                    ax.add_patch(bar)

            # outline the full cell
                    cell_border = Rectangle(
                        (row['lon_min'], row['lat_min']),
                        w, h,
                        fill=False,
                        edgecolor='black',
                        linewidth=0.8
                    )
                    ax.add_patch(cell_border)

        ax.set_title(f"{date_str_fmt} distribution")
        ax.set_xlim(68, 98)
        ax.set_ylim(6, 38)
        ax.set_aspect('equal')
        ax.axis('off')
        plt.tight_layout()
        plt.savefig(f"{output_dir}/map_bars.png", dpi=200)
        plt.close(fig)

    logging.info(f"All maps saved under the {output_dir} directory")
