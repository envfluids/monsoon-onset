#!/bin/bash
ssh derecho <<'EOF'
cd /glade/derecho/scratch/marchakitus/monsoon-onset/NeuralGCM/utils
# source /home/marchakitus/.bashrc
ml conda
conda activate npl-2025a
timeout 29m python ./pipeline.py
if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 29 minutes."
fi
EOF