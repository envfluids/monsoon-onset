#!/bin/bash

cd /net/monsoon/operational/monsoon-onset/AIFS/utils

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
else
    echo "conda not found in PATH"
    echo "PATH=$PATH"
    exit 1
fi

conda activate /net/scratch2/marchakitus/conda-envs/AIFS_ENS
python ./pipeline.py

# if [ $? -eq 124 ]; then
#   echo "ERROR Job timed out after 14 minutes."
# fi