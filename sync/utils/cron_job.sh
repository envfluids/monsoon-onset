#!/bin/bash

cd /project/pedramh/monsoon/monsoon-onset/sync/utils

source /home/marchakitus/.bashrc

conda activate /project/pedramh/monsoon/conda-envs/monsoon
timeout 2m python ./main.py

if [ $? -eq 124 ]; then
  echo "ERROR:Job timed out after 2 minutes."
fi