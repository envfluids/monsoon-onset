#!/bin/bash

cd /scratch/midway3/marchakitus/monsoon-onset/NeuralGCM/utils

source /home/marchakitus/.bashrc

conda activate /home/marchakitus/.conda/envs/neuralgcm
timeout 29m python ./pipeline.py

if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 29 minutes."
fi
