#!/bin/bash
#SBATCH -A pi-pedramh
#SBATCH -p pedramh-gpu
#SBATCH -N 1 
#SBATCH --gres=gpu:4
#SBATCH --mem=350G
#SBATCH -t 01:00:00
#SBATCH --mail-user=marchakitus@uchicago.edu
#SBATCH --mail-type=all

set -euo pipefail

# source /home/marchakitus/.bashrc

DATE_F=$"20250424T12"

conda activate /home/marchakitus/.conda/envs/AIFSv1
python ./run_model.py --date $DATE_F

conda deactivate
conda activate /home/marchakitus/.conda/envs/ncl_stable
python ./post_process.py --date $DATE_F

python ./verify_completion.py --date $DATE_F

cd ../../blend/utils
python ./main.py --date $DATE_F