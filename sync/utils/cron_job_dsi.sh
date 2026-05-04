#!/bin/bash -l

cd /net/monsoon/operational/monsoon-onset/sync/utils

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
fi

source /home/marchakitus/.bashrc

conda activate/net/scratch2/marchakitus/conda-envs/operational_pip
timeout 590s python ./main.py

if [ $? -eq 124 ]; then
  echo "ERROR: Job timed out after 9 minutes and 50 seconds."
fi