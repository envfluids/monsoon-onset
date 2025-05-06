#!/bin/bash

cd /project/pedramh/monsoon/monsoon-onset/IMERG/utils

source /home/marchakitus/.bashrc

conda activate /project/pedramh/monsoon/conda-envs/monsoon
timeout 59m python ./pipeline.py

if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 59 minutes."
fi