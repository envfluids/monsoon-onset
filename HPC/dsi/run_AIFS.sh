#!/bin/bash -l
#SBATCH -p general
#SBATCH -N 1 
#SBATCH -n 4
#SBATCH --gres=gpu:a100:1
#SBATCH --mem=64G
#SBATCH -t 01:00:00

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
fi

source /home/marchakitus/.bashrc

conda activate /net/scratch2/marchakitus/conda-envs/AIFS_ENS
python ./run_model.py --date $DATE_F --model $MODEL

conda deactivate
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./post_process.py --date $DATE_F --model $MODEL

cd ../../blend/utils
python ./main.py --date $DATE_F --model $MODEL
