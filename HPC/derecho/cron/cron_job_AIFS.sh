#!/bin/bash

ssh derecho <<'EOF'
cd /glade/derecho/scratch/marchakitus/monsoon-onset
# source /home/marchakitus/.bashrc
ml conda
conda activate /glade/work/marchakitus/conda-envs/AIFS
timeout 14m python ./HPC/utils/main.py --pipelines aifs
if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 14 minutes."
fi
EOF
