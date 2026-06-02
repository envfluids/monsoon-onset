#!/bin/bash -l
#SBATCH -p general
#SBATCH -N 1
#SBATCH -n 1
#SBATCH --cpus-per-task=2
#SBATCH --mem=32G
#SBATCH -t 00:30:00
#SBATCH --mail-user=marchakitus@uchicago.edu
#SBATCH --mail-type=all

source /home/marchakitus/.bashrc

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
fi

conda activate /net/scratch2/marchakitus/conda-envs/operational
 
python ./plot.py --date $DATE_F

python ./plot_bias.py --date $DATE_F
