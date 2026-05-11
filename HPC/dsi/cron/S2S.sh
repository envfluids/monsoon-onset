#!/bin/bash -l

LOCK_FILE="/tmp/monsoon-s2s-dsi.lock"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') S2S cron already running; skipping this tick."
    exit 0
fi

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
else
    echo "conda not found in PATH"
    echo "PATH=$PATH"
    exit 1
fi

conda activate /net/scratch2/marchakitus/conda-envs/operational

cd /net/monsoon/operational/monsoon-onset/S2S/utils
timeout 45m python /net/monsoon/operational/monsoon-onset/HPC/utils/main.py --pipelines s2s

if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 45 minutes."
fi