#!/bin/bash

cd /project/pedramh/monsoon/monsoon-onset

source /home/marchakitus/.bashrc

conda activate /home/marchakitus/.conda/envs/AIFSv1
python ./HPC/utils/main.py --pipelines aifs

# if [ $? -eq 124 ]; then
#   echo "ERROR Job timed out after 14 minutes."
# fi
