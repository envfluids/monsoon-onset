import numpy as np
import pandas as pd
import pickle
from pathlib import Path

IMERG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = IMERG_ROOT.parent
INDIA_DATA_DIR = IMERG_ROOT / "data" / "india"
SHARED_INDIA_DATA = REPO_ROOT / "blend" / "data" / "india2026" / "shared"

MATRIX_FILES = (
    INDIA_DATA_DIR / "source_to_index.pkl",
    INDIA_DATA_DIR / "target_to_index.pkl",
    INDIA_DATA_DIR / "weight_matrix.npy",
    INDIA_DATA_DIR / "threshold_array.npy",
)


def matrices_exist() -> bool:
    return all(path.exists() for path in MATRIX_FILES)


def build_matrices() -> None:
    INDIA_DATA_DIR.mkdir(parents=True, exist_ok=True)

    grid_weights_path = SHARED_INDIA_DATA / "coefs" / "subdistrict_0p25deg_weights.csv"
    df = pd.read_csv(grid_weights_path)

    unique_source = np.sort(df["source_id"].unique())
    source_to_index = {source: idx for idx, source in enumerate(unique_source)}

    unique_target = np.sort(df["target_id"].unique())
    target_to_index = {target: idx for idx, target in enumerate(unique_target)}

    weight_matrix = np.zeros((len(unique_target), len(unique_source)))
    for _, row in df.iterrows():
        source_idx = source_to_index[row["source_id"]]
        target_idx = target_to_index[row["target_id"]]
        weight_matrix[target_idx, source_idx] = row["weight"]

    with open(INDIA_DATA_DIR / "source_to_index.pkl", "wb") as f:
        pickle.dump(source_to_index, f)

    with open(INDIA_DATA_DIR / "target_to_index.pkl", "wb") as f:
        pickle.dump(target_to_index, f)

    subdistrict_thresholds = pd.read_csv(
        SHARED_INDIA_DATA / "support" / "subdistrict_thresholds.csv"
    )
    threshold_array = np.zeros(len(unique_target))
    for _, row in subdistrict_thresholds.iterrows():
        target_id = row["id"]
        if target_id in target_to_index:
            threshold_array[target_to_index[target_id]] = row["onset_thresh"]

    np.save(INDIA_DATA_DIR / "weight_matrix.npy", weight_matrix)
    np.save(INDIA_DATA_DIR / "threshold_array.npy", threshold_array)


def ensure_matrices() -> None:
    if not matrices_exist():
        build_matrices()


if __name__ == "__main__":
    build_matrices()
