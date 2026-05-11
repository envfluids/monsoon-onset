from .AIFS import plot_aifs
from .NeuralGCM import plot_neuralgcm
from .AIFS_SJI import plot_sji
import logging
logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)

def plot_circulation(base, date):
    AIFS_path = base / "AIFS" / "raw" / "output" /"AIFS" / f"init_{date}.nc"
    NGCM_path = base / "NeuralGCM" / "raw" / "output" / f"{date}"
    output_dir = base / "model_diagnostics" / "output" / "india" / f"{date}" / "circulation_plots"
    logging.info(f"AIFS path: {AIFS_path}")
    logging.info(f"NGCM path: {NGCM_path}")
    logging.info(f"Output directory: {output_dir}")
    logging.info(f"Creating directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.info("Plotting AIFS")
    plot_aifs(AIFS_path, output_dir)
    logging.info("Plotting SJI")
    plot_sji(AIFS_path, output_dir)
    logging.info("Plotting NeuralGCM")
    plot_neuralgcm(NGCM_path, output_dir)

    # logging.info("Plotting precipitation")


# def main():
#     base = Path(__file__).resolve().parent.parent.parent.parent
#     date = "20250508T06"
#     print(f"Base path: {base}")
#     print(f"Date: {date}")
#     # plot_circulation(base, date)


# if __name__ == "__main__":
#     main()
