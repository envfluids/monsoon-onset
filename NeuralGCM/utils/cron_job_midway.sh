#!/bin/bash

cd /project/pedramh/monsoon/monsoon-onset/NeuralGCM/utils

source /home/marchakitus/.bashrc

conda activate /home/marchakitus/.conda/envs/neuralgcm
timeout 15m python ./pipeline.py

if [ $? -eq 124 ]; then
  echo "ERROR NeuralGCM Job timed out after 15 minutes."
fi

cd /project/pedramh/monsoon/monsoon-onset/NeuralGCM_google/utils

conda deactivate
conda activate /project/pedramh/monsoon/conda-envs/monsoon
timeout 15m python ./pipeline.py

if [ $? -eq 124 ]; then
  echo "ERROR NeuralGCM-Google Job timed out after 15 minutes."
fi