from download_forecast import get_data
import logging
from pathlib import Path
import subprocess

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

REPO_ROOT = Path(__file__).parent.parent.parent.parent
logging.info(f"Repository root determined to be: {REPO_ROOT}")

def main():
    date_f = get_data()

    if date_f is None:
        logging.info("No new forecast downloaded, skipping pipeline execution.")
        return
    else:
        logging.info(f"New forecast downloaded with date string: {date_f}. Proceeding to run blending pipeline.")
        cmd = ["python", str(REPO_ROOT / "blend" / "utils" / "main.py"), "--date", date_f]
        subprocess.run(cmd, check=True)

if __name__ == "__main__":
    main()