#!/bin/bash -l
#SBATCH -p general
#SBATCH -N 1 
#SBATCH -n 64
#SBATCH --gres=gpu:h100:4
#SBATCH --mem=400G
#SBATCH -t 12:00:00

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
fi

source /home/marchakitus/.bashrc

conda activate /net/scratch/marchakitus/conda-envs/gencast
python ./run_gencast.py --date $DATE_F

conda deactivate
conda activate /net/scratch2/marchakitus/conda-envs/operational
# python ./post_process.py --date $DATE_F

# cd ../../blend/utils
# python ./main.py --date $DATE_F --model gencast