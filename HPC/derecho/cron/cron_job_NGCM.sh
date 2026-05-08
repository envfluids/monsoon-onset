#!/bin/bash
ssh derecho <<'EOF'
cd /glade/derecho/scratch/marchakitus/monsoon-onset
# source /home/marchakitus/.bashrc
ml conda
conda activate npl-2025a
timeout 15m python ./HPC/utils/main.py --pipelines ngcm
if [ $? -eq 124 ]; then
  echo "ERROR Job timed out after 15 minutes."
fi
cd /glade/derecho/scratch/marchakitus/monsoon-onset/NeuralGCM_google/utils
conda deactivate
conda activate /glade/work/marchakitus/conda-envs/neuralgcm
timeout 15m python ./pipeline.py
if [ $? -eq 124 ]; then
  echo "ERROR NeuralGCM-Google Job timed out after 15 minutes."
fi
EOF
