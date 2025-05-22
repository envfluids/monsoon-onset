#!/bin/bash
#PBS -A uric0009
#PBS -N AIFS_cron
#PBS -l select=1:ncpus=2:ngpus=1:mem=32GB
#PBS -l walltime=01:00:00
#PBS -q develop
#PBS -j oe

# source /home/marchakitus/.bashrc

ml conda
ml cuda
ml cdo

conda activate /glade/work/marchakitus/conda-envs/AIFS
python ./run_model.py --date $DATE_F

# conda deactivate
# conda activate /glade/work/marchakitus/conda-envs/AIFS
python ./post_process.py --date $DATE_F

# set -euo pipefail
python ./verify_completion.py --date $DATE_F

conda deactivate
conda activate /glade/work/marchakitus/conda-envs/monsoon-onset
cd ../../blend/utils
python ./main.py --date $DATE_F

cd ../../sync/utils
python ./main.py