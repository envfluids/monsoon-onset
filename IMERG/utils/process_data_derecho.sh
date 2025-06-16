#!/bin/bash
#PBS -A uric0009
#PBS -N IMERG_cron
#PBS -l select=1:ncpus=2:mem=20GB
#PBS -l walltime=00:30:00
#PBS -q develop
#PBS -j oe

# source /home/marchakitus/.bashrc
ml conda
ml cdo

conda activate /glade/work/marchakitus/conda-envs/IMD
python ./plot.py --date $DATE_F

conda deactivate
conda activate npl-2025a
python ./plot_bias.py