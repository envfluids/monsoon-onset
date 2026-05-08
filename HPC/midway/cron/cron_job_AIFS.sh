#!/bin/bash

cd /project/pedramh/monsoon/monsoon-onset/AIFS/utils

source /home/marchakitus/.bashrc

conda activate /home/marchakitus/.conda/envs/AIFSv1
python ./pipeline.py

# if [ $? -eq 124 ]; then
#   echo "ERROR Job timed out after 14 minutes."
# fi