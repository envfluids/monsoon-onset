#!/bin/bash

ssh derecho <<'EOF'
cd /glade/derecho/scratch/marchakitus/monsoon-onset/IMERG/utils
# source /home/marchakitus/.bashrc
ml conda
ml cdo
conda activate /glade/work/marchakitus/conda-envs/IMD
timeout 10m python ./pipeline.py
if [ $? -eq 124 ]; then
  echo "ERROR IMERG Job timed out after 10 minutes."
fi

conda deactivate
conda activate /glade/work/marchakitus/conda-envs/monsoon
cd /glade/derecho/scratch/marchakitus/monsoon-onset/IMD/utils
timeout 10m python ./download_IMD.py
if [ $? -eq 124 ]; then
  echo "ERROR IMD Job timed out after 10 minutes."
fi

conda deactivate
conda activate /glade/work/marchakitus/conda-envs/S2S
cd /glade/derecho/scratch/marchakitus/monsoon-onset/S2S/utils
timeout 10m python ./pipeline.py
if [ $? -eq 124 ]; then
  echo "ERROR S2S Job timed out after 10 minutes."
fi
EOF