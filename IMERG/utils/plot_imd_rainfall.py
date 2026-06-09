import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
import geopandas as gpd
import pandas as pd
import numpy as np
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

imerg_root = Path(__file__).resolve().parents[1]
repo_root = imerg_root.parent
india_data_dir = imerg_root / "data" / "india"
SHAPEFILE_DIR = (
    repo_root / "blend" / "data" / "india2026" / "shared" / "india_shapefile"
)
SUBDISTRICT_SHP = SHAPEFILE_DIR / "Sub_districts_India_ESRI.shp"
STATE_SHP = SHAPEFILE_DIR / "STATE_BOUNDARY.shp"


def load_shapefiles() -> tuple:
    subdistrict_gdf = gpd.read_file(SUBDISTRICT_SHP)
    subdistrict_gdf["id"] = subdistrict_gdf["id"].astype(str)
    state_gdf = gpd.read_file(STATE_SHP)
    subdistrict_gdf = subdistrict_gdf.to_crs("EPSG:4326")
    state_gdf = state_gdf.to_crs("EPSG:4326")
    return subdistrict_gdf, state_gdf


def save_fig(out_dir, fig, stem: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_dir / f"{stem}.png", dpi=150, bbox_inches="tight")
    fig.savefig(out_dir / f"{stem}.pdf", bbox_inches="tight")


def build_rainfall_colorscheme() -> tuple:
    rain_bins = [0, 2, 5, 10, 20, 30, 50, 80]
    n = len(rain_bins) - 1
    rain_cmap = ListedColormap(plt.get_cmap("YlGnBu")(np.linspace(0.05, 1.0, n)))
    rain_norm = BoundaryNorm(rain_bins, ncolors=n, clip=True)
    return rain_bins, rain_cmap, rain_norm


def plot_imd_daily_precip_subdistrict(date, output_dir: Path | None = None):
    imd_subdistrict_precip_path = india_data_dir / "daily_rainfall_subdistricts.csv"
    if not imd_subdistrict_precip_path.exists():
        logger.error(f"Subdistrict precipitation CSV file not found at: {imd_subdistrict_precip_path}")
        return
    df = pd.read_csv(imd_subdistrict_precip_path)
    logger.info(f"Loaded subdistrict precipitation data with shape: {df.shape}")

    # Filter to the given date
    date_str = date.strftime("%Y%m%d")
    df = df[df["Date"] == int(date_str)]
    if df.empty:
        logger.error(f"No data found for date: {date_str}")
        return

    # Handle duplicate rows for same date — keep only the last one
    if len(df) > 1:
        logger.warning(f"Multiple rows found for date {date_str}, taking the last one")
        df = df.tail(1)
    logger.info(f"Filtered data for date: {date_str}, shape: {df.shape}")

    # Melt from wide to long format
    # wide: one row, columns are subdistrict IDs
    # long: one row per subdistrict with id and rainfall value
    df_long = df.drop(columns=["Date"]).T.reset_index()
    logger.info(f"Transposed shape: {df_long.shape}, columns: {df_long.columns.tolist()[:5]}")
    df_long = df_long.iloc[:, -2:]  # take only last 2 columns
    df_long.columns = ["id", "rainfall"]
    df_long["id"] = df_long["id"].astype(str)
    df_long["rainfall"] = pd.to_numeric(df_long["rainfall"], errors="coerce")

    logger.info("Loading Shapefiles for plotting...")
    subdistrict_gdf, state_gdf = load_shapefiles()

    # Merge shapefile with rainfall data
    merged_gdf = subdistrict_gdf.merge(df_long, on="id", how="left")

    # Build color scheme
    rain_bins, rain_cmap, rain_norm = build_rainfall_colorscheme()

    # Plot
    fig, ax = plt.subplots(figsize=(10, 12))

    # Gray for subdistricts with no data
    no_data_gdf = merged_gdf[merged_gdf["rainfall"].isna()]
    if not no_data_gdf.empty:
        no_data_gdf.plot(ax=ax, color="#d3d3d3", linewidth=0.0)

    # Colored for subdistricts with data
    data_gdf = merged_gdf[merged_gdf["rainfall"].notna()].copy()
    if not data_gdf.empty:
        data_gdf["color"] = data_gdf["rainfall"].apply(lambda v: rain_cmap(rain_norm(v)))
        data_gdf.plot(ax=ax, color=data_gdf["color"].tolist(), linewidth=0.1, edgecolor="none")

    # Draw boundaries
    merged_gdf.boundary.plot(ax=ax, linewidth=0.1, edgecolor="#888888")
    state_gdf.boundary.plot(ax=ax, linewidth=1.2, edgecolor="black")

    # Colorbar
    sm = plt.cm.ScalarMappable(norm=rain_norm, cmap=rain_cmap)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, orientation="vertical", fraction=0.03, pad=0.04)
    cbar.set_label("Daily Rainfall (mm)")

    ax.set_title(f"IMD Subdistrict Daily Rainfall – {date_str}", fontsize=13)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")

    plt.tight_layout()
    out_dir = output_dir or (imerg_root / "output" / date.strftime("%Y%m%d"))
    if not out_dir.exists():
        logger.info(f"Output directory does not exist, creating: {out_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)
    save_fig(out_dir, fig, f"imd_daily_precip_{date_str}")
    plt.close(fig)
    logger.info(f"Map saved to {out_dir}")
