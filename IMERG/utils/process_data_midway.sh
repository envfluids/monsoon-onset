#!/bin/bash
#SBATCH -A pi-pedramh
#SBATCH -p pedramh-gpu
#SBATCH -N 1
#SBATCH -n 32
#SBATCH --mem=350G
#SBATCH -t 00:30:00
#SBATCH --mail-user=marchakitus@uchicago.edu
#SBATCH --mail-type=all

source /home/marchakitus/.bashrc

conda activate /project/pedramh/monsoon/conda-envs/monsoon
python ./plot.py --date $DATE_F

conda deactivate
conda activate /home/marchakitus/.conda/envs/ncl_stable
python ./plot_bias.py
