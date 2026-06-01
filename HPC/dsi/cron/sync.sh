#!/bin/bash -l

LOCK_FILE="/tmp/monsoon-sync-dsi.lock"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') sync cron already running; skipping this tick."
    exit 0
fi

cd /net/monsoon/operational/monsoon-onset/sync/utils

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
fi

source /home/marchakitus/.bashrc

conda activate /net/scratch2/marchakitus/conda-envs/operational_pip
python ./main.py
# timeout 3600s python ./main.py

# if [ $? -eq 124 ]; then
#   echo "ERROR: Job timed out after 1 hour."
# fi