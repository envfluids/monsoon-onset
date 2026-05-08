#!/bin/bash -l
set -euo pipefail

LOCK_FILE="/tmp/monsoon-aifs-dsi.lock"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') AIFS cron already running; skipping this tick."
    exit 0
fi

cd /net/monsoon/operational/monsoon-onset/AIFS/utils

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
else
    echo "conda not found in PATH"
    echo "PATH=$PATH"
    exit 1
fi

conda activate /net/scratch2/marchakitus/conda-envs/AIFS_ENS
timeout 1200s python ./pipeline.py

if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 20 minutes."
fi
