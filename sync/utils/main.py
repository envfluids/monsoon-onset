import os
from pathlib import Path
from glob import glob
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(message)s')

base = Path(__file__).resolve().parent.parent.parent
operational_dir = base.parent / "monsoon-operational"
live_dir = operational_dir / "docs" / "assets" 
maps_dir = live_dir / "images"
data_dir = live_dir / "data"

latest = base / "sync" / "latest"
latest_dir = glob(str(latest / "*"))[0]
date = latest_dir.split("/")[-1]

live_date_ref = data_dir / "latest.txt"
if not os.path.exists(live_date_ref):
    logging.info(f"Creating live date reference file at {live_date_ref}")
    with open(live_date_ref, "w") as f:
        f.write(date)

with open(live_date_ref, "r") as f:
    live_date = f.read().strip()

if date == live_date:
    logging.info(f"Latest date {date} is the same as live date {live_date}. No need to update.")
else:
    logging.info(f"Latest date {date} is different from live date {live_date}. Updating live date.")

    command = f"rm -r {maps_dir}/*"
    os.system(command)
    command = f"rm -r {data_dir}/*"
    os.system(command)

    latest_maps = latest_dir + "/maps" + "/map_bars.png"
    command = f"cp {latest_maps} {maps_dir}"
    print(command)
    os.system(command)

    latest_data = latest_dir + "/blend_output_summary.csv"
    command = f"cp {latest_data} {data_dir}"
    os.system(command)

    with open(data_dir / "latest.txt", "w") as f:
        f.write(date)

    logging.info(f"Updated live date to {date}.")

    command = f"cd {operational_dir} && git add . && git commit -m 'Updated live date to {date}' && git push"
    os.system(command)
    logging.info(f"Pushed changes to operational repo.")