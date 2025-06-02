#!/bin/bash
#SBATCH -A pi-pedramh
#SBATCH -p pedramh-gpu
#SBATCH -N 1 
#SBATCH -n 4
#SBATCH --mem=100G
#SBATCH -t 01:00:00
#SBATCH --mail-user=marchakitus@uchicago.edu
#SBATCH --mail-type=all

source /home/marchakitus/.bashrc

conda activate /home/marchakitus/.conda/envs/ncl_stable
python ./post_process.py --date $DATE_F


cd ../../blend/utils
python ./main.py --date $DATE_F --source google