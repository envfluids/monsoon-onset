#!/bin/bash
#PBS -A uric0009
#PBS -l select=1:ncpus=1:mem=6GB
#PBS -l walltime=01:00:00
#PBS -q develop
#PBS -j oe

# source /home/marchakitus/.bashrc

hostname -f

ml conda
ml cdo

conda activate npl-2025a
python ./process_forecast.py --date $DATE_F

conda deactivate
conda activate /glade/work/marchakitus/conda-envs/monsoon
cd ../../sync/utils
python ./sync_S2S.py --date $DATE_F