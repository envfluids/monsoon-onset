#!/bin/bash
#PBS -A uric0009
#PBS -l select=1:ncpus=32:ngpus=1:mem=100GB
#PBS -l walltime=01:00:00
#PBS -q develop
#PBS -j oe

# source /home/marchakitus/.bashrc

ml conda
ml cuda
ml cdo
ml ncl

# conda activate /home/marchakitus/.conda/envs/ncl_stable
conda activate /glade/work/marchakitus/conda-envs/neuralgcm
python ./preprocess.py --date $DATE_F

# conda deactivate
# conda activate /glade/work/marchakitus/conda-envs/neuralgcm
python ./run_model.py --date $DATE_F
# python ./run_model.py --date $DATE_F --mpi 0 &
# python ./run_model.py --date $DATE_F --mpi 1 &
# python ./run_model.py --date $DATE_F --mpi 2 &
# python ./run_model.py --date $DATE_F --mpi 3 &

# wait

conda deactivate
conda activate npl-2025a
python ./post_process.py --date $DATE_F

# conda deactivate
# conda activate /home/marchakitus/.conda/envs/neuralgcm
python ./post_process_merge.py --date $DATE_F

# set -euo pipefail
python ./verify_completion.py --date $DATE_F

conda deactivate
conda activate /glade/work/marchakitus/conda-envs/monsoon-onset

cd ../../blend/utils
python ./main.py --date $DATE_F

cd ../../sync/utils
python ./main.py