#!/bin/bash

cd /net/monsoon/operational/monsoon-onset

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
else
    echo "conda not found in PATH"
    echo "PATH=$PATH"
    exit 1
fi

conda activate /net/scratch2/marchakitus/conda-envs/operational
timeout 15m python ./HPC/utils/main.py --pipelines ngcm

if [ $? -eq 124 ]; then
  echo "ERROR NeuralGCM Job timed out after 15 minutes."
fi

# cd /net/monsoon/operational/monsoon-onset/NeuralGCM_google/utils

# conda deactivate
# conda activate /net/scratch2/marchakitus/conda-envs/operational
# timeout 15m python ./pipeline.py

# if [ $? -eq 124 ]; then
#   echo "ERROR NeuralGCM-Google Job timed out after 15 minutes."
# fi
