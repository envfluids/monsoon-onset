#!/bin/bash
#PBS -A uric0009
#PBS -N NGCM_g
#PBS -l select=1:ncpus=2:mem=20GB
#PBS -l walltime=00:30:00
#PBS -q develop
#PBS -j oe

ml conda
ml cdo

conda activate /glade/work/marchakitus/conda-envs/neuralgcm
python ./post_process.py --date $DATE_F


conda deactivate
conda activate /glade/work/marchakitus/conda-envs/monsoon-onset
cd ../../blend/utils
python ./main.py --date $DATE_F --source google