from datetime import timedelta
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Patch
from matplotlib.colors import TwoSlopeNorm, LinearSegmentedColormap, BoundaryNorm, ListedColormap
import geopandas as gpd
from shapely.geometry import Polygon
from pathlib import Path
import logging

def make_maps(summary, date):
    base = Path(__file__).resolve().parent.parent
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


def make_extra_maps(summary, date):
    base = Path(__file__).resolve().parent.parent
    india_shapefile = base / "data" / "india_shapefile" / "India_Country_Boundary.shp"
    
    # ------------------------------------------------------------------------------
    # 1) Ensure output folder exists
    # ------------------------------------------------------------------------------
    output_dir = base / "output" / date / "maps" / "extra"
    os.makedirs(output_dir, exist_ok=True)

    preds_df = summary.copy()

    # 2) Forecast & Climatology fields
    for i in range(1,5):
        preds_df[f'Climatology_p_{i}'] = preds_df[f'clim_week{i}']
        preds_df[f'Forecast_p_{i}']   = preds_df[f'week{i}']
    preds_df['Forecast_p_later']    = preds_df.get('later', 1 - preds_df[[f'Forecast_p_{i}' for i in range(1,5)]].sum(axis=1))
    preds_df['Climatology_p_later'] = 1 - preds_df[[f'Climatology_p_{i}' for i in range(1,5)]].sum(axis=1)

    # 3) Cell corners
    preds_df['lon_min'] = preds_df['lon'] - 1
    preds_df['lon_max'] = preds_df['lon'] + 1
    preds_df['lat_min'] = preds_df['lat'] - 1
    preds_df['lat_max'] = preds_df['lat'] + 1

    # 4) India boundary and extents
    india_gdf = gpd.read_file(india_shapefile).to_crs("EPSG:4326")
    minx, miny, maxx, maxy = india_gdf.total_bounds
    x_min, x_max = minx, maxx
    y_min, y_max = miny, maxy

    # 5) Color definitions using matplotlib's plasma scheme
    period_order = ['just_week1','weeks12','weeks23','weeks34','weeks4later','later']
    plasma_cmap = plt.get_cmap('plasma')
    stops = np.linspace(0.2, 1.0, len(period_order))
    period_colors = {k: plasma_cmap(s) for k, s in zip(period_order, stops)}
    period_colors['none'] = '#d3d3d3'

    # 6) Week probability maps with red/green outlines and legend
    prob_bins = [0,0.1,0.2,0.3,0.4,1.0]
    prob_cmap = ListedColormap(plt.get_cmap('plasma_r')(np.linspace(0,1,len(prob_bins)-1)))
    prob_norm = BoundaryNorm(prob_bins, ncolors=len(prob_bins)-1, clip=True)

    for t, grp in preds_df.groupby('time'):
        ds = t.strftime('%Y-%m-%d')
        week_titles = {i: f"{(t + timedelta(days=(i-1)*7+1)).strftime('%m/%d/%Y')} - {(t + timedelta(days=(i-1)*7+7)).strftime('%m/%d/%Y')}" for i in range(1,5)}
        fig, axes = plt.subplots(1,4,figsize=(18,5),sharex=True,sharey=True,gridspec_kw={'wspace':0.03})
        for i, ax in enumerate(axes,1):
            india_gdf.boundary.plot(ax=ax,linewidth=0.5,edgecolor='black')
            for _, r in grp.iterrows():
                v = r[f'Forecast_p_{i}']
                if pd.isna(v): continue
                lon0, lat0 = r['lon_min'], r['lat_min']
                w, h = r['lon_max']-lon0, r['lat_max']-lat0
                ax.add_patch(Rectangle((lon0,lat0),w,h,facecolor=prob_cmap(prob_norm(v)),edgecolor='none',zorder=1))
                clim = r[f'Climatology_p_{i}']
                if v <= clim - 0.10:
                    ec, lw, zo = 'red', 3, 3
                elif v >= clim + 0.10:
                    ec, lw, zo = 'green', 3, 3
                else:
                    continue
                ax.add_patch(Rectangle((lon0,lat0),w,h,fill=False,edgecolor=ec,linewidth=lw,zorder=zo))
            ax.set_title(week_titles[i],fontsize=10,pad=6)
            ax.set_xlim(x_min,x_max); ax.set_ylim(y_min,y_max); ax.axis('off')
        sm = plt.cm.ScalarMappable(norm=prob_norm,cmap=prob_cmap)
        sm.set_array([])
        cbar = fig.colorbar(sm,ax=list(axes),orientation='horizontal',fraction=0.04,pad=0.08)
        cbar.set_label('Probability')
        outline_handles = [
            Patch(facecolor='none',edgecolor='red',linewidth=3,label='≥10% lower than climatology'),
            Patch(facecolor='none',edgecolor='green',linewidth=3,label='≥10% higher than climatology')
        ]
        fig.legend(handles=outline_handles,loc='lower right',bbox_to_anchor=(0.98,0.02))
        fig.subplots_adjust(left=0.02,right=0.98,top=0.90,bottom=0.15)
        logging.info(f"Saving max prob map for {ds}")
        plt.savefig(f"{output_dir}/prob_weeks1-4_{ds}.png",dpi=150)
        plt.close(fig)

    # 7) Combined bar-chart map using plasma colors with date labels and 'Uncertain'
    for t, grp in preds_df.groupby('time'):
        ds = t.strftime('%Y-%m-%d')
        pdays = {'just_week1':(1,7),'weeks12':(1,14),'weeks23':(8,21),'weeks34':(15,28),'weeks4later':(22,None),'later':(29,None)}
        def max_period(vf):
            if vf[0]>=0.5:
                return 'just_week1'
            if vf[4]>=0.5:
                return 'later'
            sums = [vf[0]+vf[1],vf[1]+vf[2],vf[2]+vf[3],vf[3]+vf[4]]
            keys = ['weeks12','weeks23','weeks34','weeks4later']
            idx = int(np.argmax(sums))
            return keys[idx] if sums[idx]>=0.5 else 'none'
        fig, ax = plt.subplots(figsize=(6,6))
        india_gdf.boundary.plot(ax=ax,linewidth=0.5,edgecolor='black')
        for _, r in grp.iterrows():
            vf = [r[f'Forecast_p_{i}'] for i in range(1,5)] + [r['Forecast_p_later']]
            if any(pd.isna(vf)): continue
            cf = max_period(vf)
            lon0, lat0 = r['lon_min'],r['lat_min']
            w, h = r['lon_max']-lon0, r['lat_max']-lat0
            ax.add_patch(Rectangle((lon0,lat0),w,h,facecolor=period_colors[cf],edgecolor='black',linewidth=0.5,zorder=2))
        handles = []
        for k in period_order:
            sd,ed = pdays[k]
            lbl = f"{(t+timedelta(days=sd)).strftime('%m/%d/%Y')}" + (f" - {(t+timedelta(days=ed)).strftime('%m/%d/%Y')}" if ed else '+')
            handles.append(Patch(facecolor=period_colors[k],edgecolor='black',label=lbl))
        handles.append(Patch(facecolor=period_colors['none'],edgecolor='black',label='Uncertain'))
        fig.legend(handles=handles,title='Max Period',loc='lower left',bbox_to_anchor=(0.02,0.02),ncol=2)
        ax.set_xlim(x_min,x_max); ax.set_ylim(y_min,y_max); ax.axis('off')
        logging.info(f"Saving bar map for {ds}")
        plt.tight_layout();plt.savefig(f"{output_dir}/map_bars_{ds}.png",dpi=150);plt.close(fig)

    # 8) Individual bar plots per cell (legends removed)
    interval_map = {0:('just_week1',(1,7)),1:('week2',(8,14)),2:('week3',(15,21)),3:('week4',(22,28)),4:('later',(29,None))}
    chosen_map = {'just_week1':[0],'weeks12':[0,1],'weeks23':[1,2],'weeks34':[2,3],'weeks4later':[3,4],'later':[4]}
    for t, grp in preds_df.groupby('time'):
        ds = t.strftime('%Y-%m-%d')
        dfmt = t.strftime('%m/%d/%Y')
        for _, r in grp.iterrows():
            small_vals = [r[f'Forecast_p_{i}'] for i in range(1,5)] + [r['Forecast_p_later']]
            if any(pd.isna(small_vals)) or all(v == 0 for v in small_vals):
                continue
            # determine category
            if r['Forecast_p_later'] >= 0.5:
                cat = 'later'
            elif r['Forecast_p_1'] >= 0.5:
                cat = 'just_week1'
            else:
                two_sums = [small_vals[0]+small_vals[1], small_vals[1]+small_vals[2], small_vals[2]+small_vals[3], small_vals[3]+small_vals[4]]
                keys = ['weeks12','weeks23','weeks34','weeks4later']
                idx = int(np.argmax(two_sums))
                cat = keys[idx] if two_sums[idx] >= 0.5 else None
            if not cat:
                continue
            chosen_idx = chosen_map[cat]
            preceding_idx = [i for i in range(5) if i < chosen_idx[0]]
            after_idx = [i for i in range(5) if i > chosen_idx[-1]]
            bars, labels, cols = [], [], []
            def make_label(indices):
                sd, ed = interval_map[indices[0]][1]
                start = t + timedelta(days=sd)
                if ed:
                    return f"{start.strftime('%m/%d/%Y')} - {(t + timedelta(days=ed)).strftime('%m/%d/%Y')}"
                return f"{start.strftime('%m/%d/%Y')}+"
            def make_color(indices):
                return period_colors.get(interval_map[indices[0]][0], period_colors['none'])
            # preceding bar
            if preceding_idx:
                bars.append(sum(small_vals[i] for i in preceding_idx))
                labels.append(make_label(preceding_idx))
                cols.append(make_color(preceding_idx))
            # chosen bara
            bars.append(sum(small_vals[i] for i in chosen_idx))
            labels.append(make_label(chosen_idx))
            cols.append(period_colors.get(cat, period_colors['none']))
            # after bar
            if after_idx:
                bars.append(sum(small_vals[i] for i in after_idx))
                labels.append(make_label(after_idx))
                cols.append(make_color(after_idx))
            # plot
            fig, ax = plt.subplots(figsize=(10,6))
            ax.bar(labels, bars, color=cols, edgecolor='black')
            ax.set_ylim(0,1)
            ax.set_ylabel('Probability')
            ax.set_title(f"Cell {r['lat']:.1f},{r['lon']:.1f} on {dfmt}")
            plt.setp(ax.get_xticklabels(), rotation=45, ha='right')
            fig.tight_layout()
            fname = f"{output_dir}/bar_{r['lat']:.1f}_{r['lon']:.1f}_{ds}.png"
            logging.info(f"Saving individual bar plot for {fname.split('/')[-1]}")
            plt.savefig(fname, dpi=150)
            plt.close(fig)

    logging.info(f"All extra maps saved under the {output_dir} directory")