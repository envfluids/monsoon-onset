#!/bin/bash -l

cd /net/monsoon/operational/monsoon-onset

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
fi

source /home/marchakitus/.bashrc

conda activate /net/scratch2/marchakitus/conda-envs/operational_pip
timeout 10m python ./HPC/utils/main.py --pipelines imerg

if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 10 minutes."
fi

cd /net/monsoon/operational/monsoon-onset/IMD/utils
timeout 10m python ./download_IMD.py 

if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 10 minutes."
fi