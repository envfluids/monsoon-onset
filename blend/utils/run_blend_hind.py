import datetime
import logging
import os

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s:%(message)s"
)

start_date = datetime.datetime(2025, 5, 15, 0)

stop_date = datetime.datetime(2025, 5, 31, 0)

def check_forecasts_exist(date):
    AIFS_date = date - datetime.timedelta(hours=12)

    date_f = date.strftime('%Y%m%dT%H')
    AIFS_date_f = AIFS_date.strftime('%Y%m%dT%H')

    AIFS_exists = True
    NGCM_exists = True
    if not os.path.exists(f"/glade/derecho/scratch/marchakitus/monsoon-onset/AIFS/output/tp/tp_{AIFS_date_f}.nc"):
        print(f"FORECAST DOES NOT EXIST: AIFS forecast for {AIFS_date_f} does not exist.")
        AIFS_exists = False
    if not os.path.exists(f"/glade/derecho/scratch/marchakitus/monsoon-onset/NeuralGCM/output/tp/tp_{date_f}.nc"):
        print(f"FORECAST DOES NOT EXIST: NeuralGCM forecast for {date_f} does not exist.")
        NGCM_exists = False
    return AIFS_exists and NGCM_exists
    
date = start_date

problematic_dates = []

while date <= stop_date:
    logging.info(f"Running blend for {date.strftime('%Y%m%dT%H')}")
    if not check_forecasts_exist(date):
        logging.warning(f"Skipping blend for {date.strftime('%Y%m%dT%H')} due to missing forecasts.")
        problematic_dates.append(date.strftime('%Y%m%dT%H'))
        date += datetime.timedelta(days=1)
        continue
    os.system(f"python main.py --date {date.strftime('%Y%m%dT%H')}")
    date += datetime.timedelta(days=1)

logging.info("All blends completed.")
if problematic_dates:
    logging.warning("The following dates had missing forecasts and were skipped:")
    for d in problematic_dates:
        logging.warning(d)
else:
    logging.info("No dates had missing forecasts.")
logging.info("Script finished successfully.")