#!/bin/bash
#SBATCH -A pi-pedramh
#SBATCH -p pedramh-gpu
#SBATCH -N 1 
#SBATCH -n 32 
#SBATCH --gres=gpu:4
#SBATCH --mem=350G
#SBATCH -t 01:00:00
#SBATCH --mail-user=marchakitus@uchicago.edu
#SBATCH --mail-type=all

source /home/marchakitus/.bashrc

conda activate /home/marchakitus/.conda/envs/ncl_stable
python ./preprocess.py --date $DATE_F

conda deactivate
conda activate /home/marchakitus/.conda/envs/neuralgcm
python ./run_model.py --date $DATE_F --mpi 0 &
python ./run_model.py --date $DATE_F --mpi 1 &
python ./run_model.py --date $DATE_F --mpi 2 &
python ./run_model.py --date $DATE_F --mpi 3 &

wait

conda deactivate
conda activate /home/marchakitus/.conda/envs/ncl_stable
python ./post_process.py --date $DATE_F

conda deactivate
conda activate /home/marchakitus/.conda/envs/neuralgcm
python ./post_process_merge.py --date $DATE_F

python ./verify_completion.py --date $DATE_F

conda deactivate
conda activate /home/marchakitus/.conda/envs/ncl_stable
cd ../../blend/utils
python ./main.py --date $DATE_F