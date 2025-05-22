#!/bin/bash

ssh derecho <<'EOF'
cd /glade/derecho/scratch/marchakitus/monsoon-onset/sync/utils
# source /home/marchakitus/.bashrc
ml conda
conda activate /glade/work/marchakitus/conda-envs/monsoon
timeout 590s python ./main.py
if [ $? -eq 124 ]; then
  echo "ERROR: Job timed out after 9 minutes and 50 seconds."
fi
EOF