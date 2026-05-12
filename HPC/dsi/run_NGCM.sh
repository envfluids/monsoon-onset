#!/bin/bash -l
#SBATCH -p general
#SBATCH -N 1 
#SBATCH -n 32
#SBATCH --gres=gpu:a100:1
#SBATCH --mem=120G
#SBATCH -t 02:00:00

if command -v conda >/dev/null 2>&1; then
    eval "$(conda shell.bash hook)"
fi

conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./preprocess.py --date $DATE_F

conda deactivate
conda activate /net/scratch2/marchakitus/conda-envs/neuralgcm
# python ./run_model.py --date $DATE_F --mpi 0 &
# python ./run_model.py --date $DATE_F --mpi 1 &
# python ./run_model.py --date $DATE_F --mpi 2 &
# python ./run_model.py --date $DATE_F --mpi 3 &

python ./run_model.py --date $DATE_F

wait

conda deactivate
conda activate /net/scratch2/marchakitus/conda-envs/operational
python ./post_process.py --date $DATE_F

python ./post_process_merge.py --date $DATE_F

set -euo pipefail
python ./verify_completion.py --date $DATE_F


cd ../../model_diagnostics/utils
python ./main.py --date $DATE_F --region india

cd ../../blend/utils
python ./main.py --date $DATE_F