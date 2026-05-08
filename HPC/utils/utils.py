import json
import logging
from pathlib import Path

def get_cluster():
    base = Path(__file__).resolve().parent.parent.parent
    config_file = base / ".config" / "config.json"
    with open(config_file, "r") as f:
        config = json.load(f)
    cluster = config["cluster"]
    logging.info(f"Cluster: {cluster}")

    script_dir = base / "HPC" / cluster

    return cluster, script_dir