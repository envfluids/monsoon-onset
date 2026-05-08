#!/bin/bash

cd /project/pedramh/monsoon/monsoon-onset

source /home/marchakitus/.bashrc

conda activate /project/pedramh/monsoon/conda-envs/monsoon
timeout 10m python ./HPC/utils/main.py --pipelines imerg

if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 10 minutes."
fi

cd /project/pedramh/monsoon/monsoon-onset/IMD/utils
timeout 10m python ./download_IMD.py

if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 10 minutes."
fi
