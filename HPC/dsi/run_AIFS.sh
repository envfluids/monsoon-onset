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

REPO_ROOT="$(cd ../.. && pwd)"
export REPO_ROOT
MODEL_ENV="$(python -c 'import json, os, pathlib; root = pathlib.Path(os.environ["REPO_ROOT"]); cfg = json.load(open(root / ".config" / "envs.json")); print(cfg["dsi"]["models"][os.environ["MODEL"]])')"
DEFAULT_ENV="$(python -c 'import json, os, pathlib; root = pathlib.Path(os.environ["REPO_ROOT"]); cfg = json.load(open(root / ".config" / "envs.json")); print(cfg["dsi"]["misc"]["default"])')"

conda activate "$MODEL_ENV"
python ./run_model.py --date "$DATE_F" --model "$MODEL"

conda deactivate
conda activate "$DEFAULT_ENV"
python ./post_process.py --date "$DATE_F" --model "$MODEL"

cd ../../blend/utils
python ./main.py --date "$DATE_F" --model "$MODEL"
