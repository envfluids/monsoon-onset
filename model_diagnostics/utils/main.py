from pathlib import Path
from argparse import ArgumentParser

from india.circulation_main import plot_circulation
from india.plot_precip import plot_precip

def main():
    parser = ArgumentParser(description="Run model diagnostics utilities.")
    parser.add_argument("--date", type=str, required=False, help="Initialization date in YYYYMMDDTHH format (e.g., 20250508T06).")
    args = parser.parse_args()
    date = args.date

    base = Path(__file__).resolve().parent.parent.parent
    print(f"Base path: {base}")
    print(f"Date: {date}")
    plot_circulation(base, date)
    plot_precip(base, date)

    

if __name__ == "__main__":
    main()