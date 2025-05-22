from pathlib import Path
if __name__ == "__main__":
    from AIFS import plot_aifs
    from NeuralGCM import plot_neuralgcm
    from AIFS_SJI import plot_sji
else:
    from circulation.AIFS import plot_aifs
    from circulation.NeuralGCM import plot_neuralgcm
    from circulation.AIFS_SJI import plot_sji
import logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s"
)

def plot_circulation(base, date):
    AIFS_path = base / "AIFS" / "raw" / "output" / f"init_{date}.nc"
    NGCM_path = base / "NeuralGCM" / "raw" / "output" / f"{date}"
    output_dir = base / "blend" / "output" / f"{date}" / "circulation"
    logging.info(f"AIFS path: {AIFS_path}")
    logging.info(f"NGCM path: {NGCM_path}")
    logging.info(f"Output directory: {output_dir}")
    if not output_dir.exists():
        output_dir.mkdir(exist_ok=True)
        logging.info(f"Creating directory: {output_dir}")
    logging.info("Plotting AIFS")
    plot_aifs(AIFS_path, output_dir)
    logging.info("Plotting SJI")
    plot_sji(AIFS_path, output_dir)
    logging.info("Plotting NeuralGCM")
    plot_neuralgcm(NGCM_path, output_dir)


# def main():
#     base = Path(__file__).resolve().parent.parent.parent.parent
#     date = "20250508T06"
#     print(f"Base path: {base}")
#     print(f"Date: {date}")
#     # plot_circulation(base, date)


# if __name__ == "__main__":
#     main()