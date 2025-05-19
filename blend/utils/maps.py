from datetime import timedelta
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Patch
from matplotlib.colors import (
    TwoSlopeNorm,
    LinearSegmentedColormap,
    BoundaryNorm,
    ListedColormap,
)
import geopandas as gpd
from shapely.geometry import Polygon
from pathlib import Path
import logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s"
)

def make_maps(summary, date):
    base = Path(__file__).resolve().parent.parent
    india_shapefile = base / "data" / "india_shapefile" / "India_Country_Boundary.shp"
    india_gdf = gpd.read_file(india_shapefile).to_crs("EPSG:4326")
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

    # SECTION: Forecast & Climatology Fields ------
    for i in range(1,5):
        preds_df[f'Climatology_p_{i}'] = preds_df[f'clim_week{i}']
        preds_df[f'Forecast_p_{i}']   = preds_df[f'week{i}']
    preds_df['Forecast_p_later']    = preds_df.get('later', 1 - preds_df[[f'Forecast_p_{i}' for i in range(1,5)]].sum(axis=1))
    preds_df['Climatology_p_later'] = 1 - preds_df[[f'Climatology_p_{i}' for i in range(1,5)]].sum(axis=1)

    # SECTION: Define Cell Corners ------
    preds_df['lon_min'], preds_df['lon_max'] = preds_df['lon'] - 1, preds_df['lon'] + 1
    preds_df['lat_min'], preds_df['lat_max'] = preds_df['lat'] - 1, preds_df['lat'] + 1

    minx, miny, maxx, maxy = india_gdf.total_bounds
    x_min, x_max, y_min, y_max = minx, maxx, miny, maxy

    # SECTION: Define Color Schemes ------
    period_order = ['just_week1','weeks12','weeks23','weeks34','weeks4later','later']
    plasma_cmap = plt.get_cmap('plasma')
    stops = np.linspace(0.2, 1.0, len(period_order))
    period_colors = {k: plasma_cmap(s) for k, s in zip(period_order, stops)}
    period_colors['none'] = '#d3d3d3'
    prob_bins = [0,0.1,0.2,0.3,0.4,1.0]
    prob_cmap = ListedColormap(plt.get_cmap('plasma_r')(np.linspace(0,1,len(prob_bins)-1)))
    prob_norm = BoundaryNorm(prob_bins, ncolors=len(prob_bins)-1, clip=True)

    # SECTION: Weekly Probability Maps ------
    for t, grp in preds_df.groupby('time'):
        ds = t.strftime('%Y-%m-%d')
        week_titles = {i: f"{(t + timedelta(days=(i-1)*7+1)).strftime('%m/%d/%Y')} - {(t + timedelta(days=(i-1)*7+7)).strftime('%m/%d/%Y')}" for i in range(1,5)}
        fig, axes = plt.subplots(1,4,figsize=(18,5), sharex=True, sharey=True, gridspec_kw={'wspace':0.03})
        for i, ax in enumerate(axes, 1):
            india_gdf.boundary.plot(ax=ax, linewidth=0.5, edgecolor='black')
            for _, r in grp.iterrows():
                v = r[f'Forecast_p_{i}']
                if pd.isna(v): continue
                lon0, lat0 = r['lon_min'], r['lat_min']
                w, h = r['lon_max']-lon0, r['lat_max']-lat0
                ax.add_patch(Rectangle((lon0, lat0), w, h, facecolor=prob_cmap(prob_norm(v)), edgecolor='none', zorder=1))
                clim = r[f'Climatology_p_{i}']
                if v <= clim - 0.10:
                    ec='red'; lw=3; zo=3
                elif v >= clim + 0.10:
                    ec='green'; lw=3; zo=3
                else:
                    continue
                ax.add_patch(Rectangle((lon0, lat0), w, h, fill=False, edgecolor=ec, linewidth=lw, zorder=zo))
            ax.set_title(week_titles[i], fontsize=10, pad=6)
            ax.set_xlim(x_min, x_max); ax.set_ylim(y_min, y_max); ax.axis('off')
        sm = plt.cm.ScalarMappable(norm=prob_norm, cmap=prob_cmap)
        sm.set_array([])
        cbar = fig.colorbar(sm, ax=list(axes), orientation='horizontal', fraction=0.04, pad=0.08)
        cbar.set_label('Probability')
        legend_handles = [Patch(facecolor='none', edgecolor=c, linewidth=3, label=l) for c, l in [('red','≥10% lower than climatology'),('green','≥10% higher than climatology')]]
        fig.legend(handles=legend_handles, loc='lower right', bbox_to_anchor=(0.98,0.02))
        fig.subplots_adjust(left=0.02, right=0.98, top=0.90, bottom=0.15)
        fname = output_dir / f"prob_weeks1-4_{ds}.png"
        plt.savefig(fname, dpi=150)
        logging.info(f"Saved weekly probability map to {fname}")
        plt.close(fig)

    # SECTION: Max-Period Map & Bar-Glyph Map ------
    pdays = {'just_week1':(1,7),'weeks12':(1,14),'weeks23':(8,21),'weeks34':(15,28),'weeks4later':(22,None),'later':(29,None)}
    def max_period(vf):
        if vf[0]>=0.65: return 'just_week1'
        if vf[4]>=0.65: return 'later'
        sums=[vf[0]+vf[1], vf[1]+vf[2], vf[2]+vf[3], vf[3]+vf[4]]
        keys=['weeks12','weeks23','weeks34','weeks4later']
        idx=int(np.argmax(sums))
        return keys[idx] 

    for t, grp in preds_df.groupby('time'):
        ds = t.strftime('%Y-%m-%d')
        # Max-period
        fig, ax = plt.subplots(figsize=(6,6))
        india_gdf.boundary.plot(ax=ax, linewidth=0.5, edgecolor='black')
        for _, r in grp.iterrows():
            vf=[r[f'Forecast_p_{i}'] for i in range(1,5)]+[r['Forecast_p_later']]
            if any(pd.isna(vf)): continue
            cf=max_period(vf)
            lon0,lat0, w,h = r['lon_min'], r['lat_min'], r['lon_max']-r['lon_min'], r['lat_max']-r['lat_min']
            ax.add_patch(Rectangle((lon0,lat0), w,h, facecolor=period_colors[cf], edgecolor='black', linewidth=0.5, zorder=2))
        handles=[Patch(facecolor=period_colors[k], edgecolor='black', label=f"{(t+timedelta(days=pdays[k][0])).strftime('%m/%d/%Y')}{(' - '+(t+timedelta(days=pdays[k][1])).strftime('%m/%d/%Y')) if pdays[k][1] else '+'}") for k in period_order]
        handles.append(Patch(facecolor=period_colors['none'], edgecolor='black', label='Uncertain'))
        fig.legend(handles=handles, title='Max Period', loc='lower left', bbox_to_anchor=(0.02,0.02), ncol=2)
        ax.set_xlim(x_min, x_max); ax.set_ylim(y_min, y_max); ax.axis('off')
        fname = output_dir / f"map_max_period_{ds}.png"
        plt.tight_layout(); plt.savefig(fname,dpi=150); plt.close(fig)
        logging.info(f"Saved max-period map to {fname}")
        # Bar-glyph
        fig2, ax2 = plt.subplots(figsize=(6,6))
        india_gdf.boundary.plot(ax=ax2, linewidth=0.5, edgecolor='black')
        for _, r in grp.iterrows():
            vf=[r[f'Forecast_p_{i}'] for i in range(1,5)]+[r['Forecast_p_later']]
            if any(pd.isna(vf)): continue
            cf=max_period(vf)
            lon0,lat0, w,h = r['lon_min'],r['lat_min'],r['lon_max']-r['lon_min'],r['lat_max']-r['lat_min']
            ax2.add_patch(Rectangle((lon0,lat0), w,h, facecolor=period_colors[cf], edgecolor='black', linewidth=0.5, zorder=2))
        for _, r in grp.iterrows():
            probs=[r[f'Forecast_p_{i}'] for i in range(1,5)]+[r['Forecast_p_later']]
            if any(pd.isna(probs)): continue
            lon0,lat0,w,h=r['lon_min'],r['lat_min'],r['lon_max']-r['lon_min'],r['lat_max']-r['lat_min']
            n=len(probs); bw=(w*0.8)/n; spacing=(w*0.2)/(n+1)
            for pi,p in enumerate(probs):
                bx=lon0+spacing+pi*(bw+spacing); by=lat0; bar_h=h*p
                ax2.add_patch(Rectangle((bx,by), bw, bar_h, facecolor='black', edgecolor='black', linewidth=0.3, zorder=3))
        fig2.legend(handles=handles, title='Max Period', loc='lower left', bbox_to_anchor=(0.02,0.02), ncol=2)
        ax2.set_xlim(x_min, x_max); ax2.set_ylim(y_min, y_max); ax2.axis('off')
        fname = output_dir / f"map_bars_with_probs_{ds}.png"
        plt.tight_layout(); plt.savefig(fname,dpi=150); plt.close(fig2)
        logging.info(f"Saved bar-glyph map to {fname}")
        # Zoomed Regions
        regions={
            'Odisha': [(18,82),(20,82),(20,84),(22,84),(22,86)],
            'Telengana': [(18,78),(20,78),(18,80),(20,80)]
        }
        for name, centers in regions.items():
            lats=[c[0] for c in centers]; lons=[c[1] for c in centers]
            lat_min_r=min(lats)-1; lat_max_r=max(lats)+1
            lon_min_r=min(lons)-1; lon_max_r=max(lons)+1
            figR, axR = plt.subplots(figsize=(6,6))
            india_gdf.boundary.plot(ax=axR, linewidth=0.5, edgecolor='black')
            for _, r in grp.iterrows():
                vf=[r[f'Forecast_p_{i}'] for i in range(1,5)]+[r['Forecast_p_later']]
                if any(pd.isna(vf)): continue
                cf=max_period(vf)
                lon0,lat0,w,h=r['lon_min'],r['lat_min'],r['lon_max']-r['lon_min'],r['lat_max']-r['lat_min']
                axR.add_patch(Rectangle((lon0,lat0), w,h, facecolor=period_colors[cf], edgecolor='black', linewidth=0.5, zorder=2))
            for _, r in grp.iterrows():
                probs=[r[f'Forecast_p_{i}'] for i in range(1,5)] + [r['Forecast_p_later']]
                if any(pd.isna(probs)): continue
                lon0,lat0,w,h=r['lon_min'],r['lat_min'],r['lon_max']-r['lon_min'],r['lat_max']-r['lat_min']
                n=len(probs); bw=(w*0.8)/n; spacing=(w*0.2)/(n+1)
                for pi,p in enumerate(probs):
                    bx=lon0+spacing+pi*(bw+spacing); by=lat0; bar_h=h*p
                    axR.add_patch(Rectangle((bx,by), bw, bar_h, facecolor='black', edgecolor='black', linewidth=0.3, zorder=3))
            # Draw blue outlines for each box center
            for center in centers:
                lat_c, lon_c = center
                axR.add_patch(Rectangle((lon_c-1, lat_c-1), 2, 2, fill=False, edgecolor='blue', linewidth=4, zorder=4))
            region_patch = Patch(facecolor='none', edgecolor='blue', linewidth=4, label=name)
            all_handles = handles + [region_patch]
            # Conditional legend placement
            if name == 'Odisha':
                legend_loc = 'lower right'
                bbox = (0.98,0.02)
            else:
                legend_loc = 'lower left'
                bbox = (0.02,0.02)
            figR.legend(handles=all_handles, title='Legend', loc=legend_loc, bbox_to_anchor=bbox, ncol=1)
            axR.set_xlim(lon_min_r-3, lon_max_r+3)
            axR.set_ylim(lat_min_r-3, lat_max_r+3)
            axR.axis('off'); plt.tight_layout()
            fname = output_dir / f"map_bars_with_probs_{name}_{ds}.png"
            plt.savefig(fname,dpi=150); plt.close(figR)
            logging.info(f"Saved zoomed region map to {fname}")

    # SECTION: Individual Bar Plots per Cell ------
    interval_map={0:('just_week1',(1,7)),1:('week2',(8,14)),2:('week3',(15,21)),3:('week4',(22,28)),4:('later',(29,None))}
    chosen_map={'just_week1':[0],'weeks12':[0,1],'weeks23':[1,2],'weeks34':[2,3],'weeks4later':[3,4],'later':[4]}
    for t, grp in preds_df.groupby('time'):
        ds=t.strftime('%Y-%m-%d'); dfmt=t.strftime('%m/%d/%Y')
        for _, r in grp.iterrows():
            small_vals=[r[f'Forecast_p_{i}'] for i in range(1,5)] + [r['Forecast_p_later']]
            if any(pd.isna(small_vals)) or all(v==0 for v in small_vals): continue
            if r['Forecast_p_later']>=0.5: cat='later'
            elif r['Forecast_p_1']>=0.5: cat='just_week1'
            else:
                two_sums=[small_vals[i]+small_vals[i+1] for i in range(4)]
                keys=list(chosen_map.keys())[1:5]; idx=int(np.argmax(two_sums))
                cat=keys[idx] if two_sums[idx]>=0.5 else None
            if not cat: continue
            chosen_idx=chosen_map[cat]
            preceding_idx=[i for i in range(5) if i<chosen_idx[0]]
            after_idx=[i for i in range(5) if i>chosen_idx[-1]]
            bars,labels,cols=[],[],[]
            def make_label(indices):
                sd,ed=interval_map[indices[0]][1]; start=t+timedelta(days=sd)
                return f"{start.strftime('%m/%d/%Y')}{(' - '+(t+timedelta(days=ed)).strftime('%m/%d/%Y')) if ed else '+'}"
            def make_color(indices): return period_colors.get(interval_map[indices[0]][0],period_colors['none'])
            if preceding_idx:
                bars.append(sum(small_vals[i] for i in preceding_idx))
                labels.append(make_label(preceding_idx)); cols.append(make_color(preceding_idx))
            bars.append(sum(small_vals[i] for i in chosen_idx)); labels.append(make_label(chosen_idx)); cols.append(period_colors.get(cat,period_colors['none']))
            if after_idx:
                bars.append(sum(small_vals[i] for i in after_idx)); labels.append(make_label(after_idx)); cols.append(make_color(after_idx))
            fig, ax = plt.subplots(figsize=(10,6))
            ax.bar(labels, bars, color=cols, edgecolor='black'); ax.set_ylim(0,1)
            ax.set_ylabel('Probability'); ax.set_title(f"Cell {r['lat']:.1f},{r['lon']:.1f} on {dfmt}")
            plt.setp(ax.get_xticklabels(), rotation=45, ha='right'); fig.tight_layout()
            individual_bar_plots_dir = output_dir / "individual_bar_plots"
            os.makedirs(individual_bar_plots_dir, exist_ok=True)
            fname= individual_bar_plots_dir / f"bar_{r['lat']:.1f}_{r['lon']:.1f}_{ds}.png"
            plt.savefig(fname,dpi=150); plt.close(fig)
            logging.info(f"Saved individual bar plot to {fname}")
    logging.info(f"Maps and bar plots saved under {output_dir}")
