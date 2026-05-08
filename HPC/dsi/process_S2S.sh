#!/bin/bash -l
#SBATCH -p general
#SBATCH -N 1
#SBATCH -n 2
#SBATCH --mem=32G
#SBATCH -t 00:30:00
#SBATCH --mail-user=marchakitus@uchicago.edu
#SBATCH --mail-type=all
#SBATCH --qos=protected

source /home/marchakitus/.bashrc

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
fi

conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./process_forecast.py --date $DATE_F

# conda deactivate
# conda activate /glade/work/marchakitus/conda-envs/monsoon
# cd ../../sync/utils
# python ./sync_S2S.py --date $DATE_F