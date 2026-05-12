from pathlib import Path
from argparse import ArgumentParser

from india.circulation_main import plot_circulation as plot_circulation_india
from india.plot_precip import plot_precip as plot_precip_india

def main():
    parser = ArgumentParser(description="Run model diagnostics utilities.")
    parser.add_argument("--date", type=str, required=False, help="Initialization date in YYYYMMDDTHH format (e.g., 20250508T06).")
    parser.add_argument("--region", type=str, required=False, help="Region for which to run diagnostics.")
    args = parser.parse_args()
    date = args.date
    region = args.region

    base = Path(__file__).resolve().parent.parent.parent
    print(f"Base path: {base}")
    print(f"Date: {date}")
    if region == "india":
        plot_circulation_india(base, date)
        plot_precip_india(base, date)
    else:
        print(f"Region '{region}' not recognized. No diagnostics will be run.")

    

if __name__ == "__main__":
    main()