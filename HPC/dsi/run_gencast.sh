#!/bin/bash -l
#SBATCH -p Monsoon
#SBATCH -N 1 
#SBATCH -n 1
#SBATCH --cpus-per-task=32
#SBATCH --gres=gpu:h200:4
#SBATCH --mem=350G
#SBATCH -t 12:00:00

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
fi

source /home/marchakitus/.bashrc
unset LD_LIBRARY_PATH
conda activate /net/scratch/marchakitus/conda-envs/gencast
python ./run_gencast.py --date $DATE_F

conda deactivate
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./post_process.py --date $DATE_F

# cd ../../blend/utils
# python ./main.py --date $DATE_F --model gencast