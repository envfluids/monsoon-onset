#!/bin/bash -l
#SBATCH -p general
#SBATCH -N 1 
#SBATCH -n 1
#SBATCH --cpus-per-task=16
#SBATCH --gres=gpu:a100:4
#SBATCH --mem=120G
#SBATCH -t 01:00:00

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
fi

conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./preprocess_ic.py --date $DATE_F

conda deactivate
conda activate /net/scratch2/marchakitus/conda-envs/neuralgcm
python ./run_model.py --date $DATE_F

conda deactivate
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./post_process.py --date $DATE_F

python ./post_process_merge.py --date $DATE_F

cd ../../blend/utils
python ./main.py --date $DATE_F --model NeuralGCM
