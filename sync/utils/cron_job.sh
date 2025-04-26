#!/bin/bash

cd /scratch/midway3/marchakitus/monsoon-onset/sync/utils

source /home/marchakitus/.bashrc

# conda activate /home/marchakitus/.conda/envs/ncl_stable
timeout 2m python ./main.py

if [ $? -eq 124 ]; then
  echo "ERROR:Job timed out after 2 minutes."
fi