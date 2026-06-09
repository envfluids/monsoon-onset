import argparse
import datetime
import fcntl
import logging
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

# from operational.utils.kiremt_onset_messages import generate_messages
from operational.utils.remap_nc import batch_aggregate_to_adm3_matrix

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)

REPO_BASE = Path(__file__).parent.parent.parent.parent
TARGET_WORK_DIR = Path(__file__).parent / "operational"
LOCK_FILE = Path(__file__).with_suffix(".lock")

ALLOWED_DELETE_EXT = {".csv", ".nc", ".pkl", ".png"}

KEEP_F_NAMES = [
    "imd_clim_mok_date_clim_issue.pkl",
    "imd_clim_mok_date_clim_unc_issue.pkl",
]


@contextmanager
def ethiopia_pipeline_lock():
    """Allow only one Ethiopia blending pipeline process to run at a time."""
    # Keep the coordination file in place: unlinking it can let a third process
    # lock a new inode while a waiter still holds the old one.
    with LOCK_FILE.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info(
                "Another Ethiopia blending process is running. Waiting for lock: %s",
                LOCK_FILE,
            )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()}\n")
        lock_file.flush()
        logging.info("Acquired Ethiopia blending lock: %s", LOCK_FILE)

        try:
            yield
        finally:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.flush()
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            logging.info("Released Ethiopia blending lock: %s", LOCK_FILE)


def move_and_rename_files(src, dest, f_dict):
    for f_name, rename in f_dict.items():
        expected_path = src / f_name
        if not expected_path.exists():
            logging.error(f"Expected output file {expected_path} does not exist.")
            raise FileNotFoundError(
                f"Expected output file {expected_path} does not exist. Check the step's logs above for errors."
            )
        else:
            new_path = dest / rename
            expected_path.rename(new_path)
            logging.info(f"Moved and renamed {expected_path} to {new_path}")

def move_dir(src, dest):
    """Move all files from src to dest."""
    for f in src.glob("*"):
        if f.is_file():
            new_path = dest / f.name
            f.rename(new_path)
            logging.info(f"Moved {f} to {new_path}")

def delete_all_files_in_dir(dir_path):
    for f in dir_path.glob("*"):
        if f.is_file() and f.suffix in ALLOWED_DELETE_EXT:
            if f.name in KEEP_F_NAMES:
                logging.info(f"Keeping file {f} as it is in the keep list.")
                continue
            else:
                f.unlink()
                logging.info(f"Deleted file {f}")


def ensure_all_paths_exist(paths):
    """Return true if all paths exist, false otherwise."""
    return all(path.exists() for path in paths)


def ensure_only_keep_files_exist(dir_path):
    """Return true i.f.f. all files in the directory are in the keep list."""
    for f in dir_path.glob("*"):
        if f.is_file() and f.name not in KEEP_F_NAMES:
            return False
    return True


def get_blend_params(deterministic_model, ensemble_model, onset_definition):
    blend_name = f"{deterministic_model}_{ensemble_model}"

    not_implemented = False
    if blend_name == "AIFS_single_v1p1_AIFS_ENS_v1":
        spec_dict = {
            "--model_single": "aifs",
            "--model_ens": "aifs_ens",
            "--aifs_spec": "aifs_2026",
            "--aifs_ens_spec": "aifs_ens_2026",
            "--combine_spec": "combine_template_clim_mok_date_2026",
            "--connect_spec": "connect_clim_mok_date_2026",
            "--blend_spec": "cv_models_clim_mok_date_2026",
        }
        if onset_definition == "ICPAC":
            spec_dict["--coef_dir"] = "Monsoon_Data/results/dry_spell_aifs_aifs_ens"
            spec_dict["--gt_path"] = (
                "Monsoon_Data/Processed_Data/Models/dry_spell_v1/imd_clim_mok_date_wide.pkl"
            )
        if onset_definition == "2mm":
            not_implemented = True
        remapping_file = (
            TARGET_WORK_DIR / "Monsoon_Data" / "grid_to_district_mapping_v1.csv"
        )

    if blend_name == "AIFS_single_v2_AIFS_ENS_v2":
        spec_dict = {
            "--model_single": "aifs_v2",
            "--model_ens": "aifs_ens_v2",
            "--aifs_spec": "aifs_2026",
            "--aifs_ens_spec": "aifs_ens_2026",
            "--combine_spec": "combine_template_clim_mok_date_2026",
            "--connect_spec": "connect_clim_mok_date_2026",
            "--blend_spec": "cv_models_clim_mok_date_2026",
        }

        if onset_definition == "ICPAC":
            spec_dict["--coef_dir"] = (
                "Monsoon_Data/results/dry_spell_aifs_v2_aifs_ens_v2"
            )
            spec_dict["--gt_path"] = (
                "Monsoon_Data/Processed_Data/Models/dry_spell_v2/imd_clim_mok_date_wide.pkl"
            )
        if onset_definition == "2mm":
            spec_dict["--coef_dir"] = (
                "Monsoon_Data/Processed_Data/train_aifs_v2_aifs_ens_v2_2mm/results"
            )
            spec_dict["--gt_path"] = (
                "Monsoon_Data/Processed_Data/train_aifs_v2_aifs_ens_v2_2mm/imd_clim_mok_date_2mm_wide.pkl"
            )

        remapping_file = (
            TARGET_WORK_DIR / "Monsoon_Data" / "grid_to_district_mapping.csv"
        )

    if blend_name == "AIFS_single_v2_NeuralGCM":
        spec_dict = {
            "--model_single": "aifs_v2",
            "--model_ens": "ngcm",
            "--aifs_spec": "aifs_2026",
            "--aifs_ens_spec": "ngcm_2026",
            "--combine_spec": "combine_template_clim_mok_date_2026_ngcm",
            "--connect_spec": "connect_clim_mok_date_2026_ngcm",
            "--blend_spec": "cv_models_clim_mok_date_2026_ngcm",
        }
        if onset_definition == "ICPAC":
            spec_dict["--coef_dir"] = "Monsoon_Data/results/dry_spell_aifs_ngcm"
            spec_dict["--gt_path"] = (
                "Monsoon_Data/Processed_Data/Models/dry_spell_v2/imd_clim_mok_date_wide.pkl"
            )
        if onset_definition == "2mm":
            not_implemented = True

        remapping_file = (
            TARGET_WORK_DIR / "Monsoon_Data" / "grid_to_district_mapping.csv"
        )

    expected_out_dir_head = f"{blend_name}_{onset_definition}"

    return spec_dict, remapping_file, expected_out_dir_head, not_implemented


def run_blending_pipeline(
    date_f,
    ensemble_model,
    deterministic_model,
    deterministic_input=None,
    ensemble_input=None,
    output_dir=None,
    debug=False,
    skip_to=None,
):

    date = datetime.datetime.strptime(date_f, "%Y%m%dT%H")
    issue_date_f = date.strftime("%Y-%m-%d")
    year = date.year
    onset_definitions = ["ICPAC", "2mm"]
    for onset_definition in onset_definitions:
        predict_dir = TARGET_WORK_DIR / "predict"
        py_script_path = predict_dir / "run_operational_pipeline.py"

        specs, remapping_file, expected_out_dir_head, not_implemented = (
            get_blend_params(deterministic_model, ensemble_model, onset_definition)
        )

        if not_implemented:
            logging.warning(
                f"{onset_definition} onset definition not implemented for {deterministic_model}_{ensemble_model}"
            )
            continue

        expected_out_dir = (
            TARGET_WORK_DIR / "Monsoon_Data" / "Processed_Data" / expected_out_dir_head
        )
        work_dir = f"Monsoon_Data/Processed_Data/{expected_out_dir_head}"

        expected_out_dir.mkdir(parents=True, exist_ok=True)

        if ensure_only_keep_files_exist(expected_out_dir):
            logging.info(
                "Only keep files exist in the expected output directory. Proceeding with pipeline."
            )
        else:
            logging.warning(
                "Non-keep files exist in the expected output directory. Deleting all non-keep files to ensure a clean slate for the pipeline."
            )
            delete_all_files_in_dir(expected_out_dir)

        det_nc_file = Path(deterministic_input)
        det_nc_file = batch_aggregate_to_adm3_matrix(
            deterministic_model, det_nc_file, expected_out_dir, remapping_file
        )

        if ensemble_model == "NeuralGCM":
            remapping_file = TARGET_WORK_DIR / "Monsoon_Data" / "grid_to_district_mapping_ngcm.csv"

        ens_nc_file = Path(ensemble_input)
        ens_nc_file = batch_aggregate_to_adm3_matrix(
            ensemble_model, ens_nc_file, expected_out_dir, remapping_file
        )

        clim_exists = ensure_all_paths_exist(
            [expected_out_dir / f for f in KEEP_F_NAMES]
        )
        if clim_exists and skip_to is None:
            logging.info("Climatology files already exist. Skipping.")
            skip_to = 2
        if not clim_exists:
            skip_to = None
            logging.warning("Climatology files do not exist. Running full pipeline.")

        args_dict = {
            "--year": f"{year}",
            "--issue_date": issue_date_f,
            "--clim_spec": "imd_clim_mok_date_2026",
            "--coef_tag": "clim_mok_date_2022_year2022",
            "--blend_input": f"{work_dir}/cv_data_clim_mok_date_new_pipeline_2026.pkl",
            "--work_dir": work_dir,
            "--aifs_nc_file": f"{det_nc_file}",
            "--aifs_ens_nc_file": f"{ens_nc_file}",
        }

        args_dict.update(specs)

        if skip_to is not None and skip_to > 1:
            logging.info(f"Skipping to step {skip_to} in the pipeline")
            args_dict["--skip_to"] = str(skip_to)

        cmd = ["python", str(py_script_path)]
        for k, v in args_dict.items():
            cmd.extend([k, v])

        logging.info(f"Running command: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            logging.error(f"Command failed with error: {e}")
            raise RuntimeError(
                f"Pipeline failed at step with command: {' '.join(cmd)}"
            ) from e

        file_name_date_f = date.strftime("%Y%m%d")

        keep_f_names = {
            f"blend_output_summary_{file_name_date_f}.csv": f"blend_output_summary_{date_f}.csv",
            f"blended_model_global_year{year}_preds.csv": f"blend_output_{date_f}.csv",
        }

        expected_out_dir_maps = expected_out_dir / "maps"
        keep_f_names_maps = {
            f"map_max_period_{issue_date_f}_zone.png": f"zone_map_max_period_{date_f}.png",
            f"prob_weeks1-4_{issue_date_f}_zone.png": f"zone_prob_weeks1-4_{date_f}.png",
            f"max_period_index_{file_name_date_f}.nc": f"max_period_index_{date_f}.nc",
            f"weekly_probs_{file_name_date_f}.nc": f"weekly_probs_{date_f}.nc",
        }

        final_out_dir = Path(output_dir) / onset_definition
        final_out_dir.mkdir(parents=True, exist_ok=True)

        final_out_dir_hists = final_out_dir / "histograms"

        expected_out_dir_hist = expected_out_dir_maps / "histograms"

        hist_script = predict_dir / "plot_forecast_histograms.py"
        cells_file = predict_dir / "data" / "support" / "all_cells.csv"
        hist_args = {
            "--output_dir": f"{expected_out_dir_hist}",
            "--cells_file": f"{cells_file}",
            "--input_file": f"{expected_out_dir_maps / f'weekly_probs_{file_name_date_f}.nc'}",
        }

        cmd = ["python", str(hist_script)]
        for k, v in hist_args.items():
            cmd.extend([k, v])

        logging.info(f"Running command: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
            logging.info("Histograms generated successfully.")
            final_out_dir_hists.mkdir(parents=True, exist_ok=True)
            move_dir(expected_out_dir_hist, final_out_dir_hists)
        except subprocess.CalledProcessError as e:
            logging.error(f"Command failed with error: {e}")
            raise RuntimeError(
                f"Pipeline failed at step with command: {' '.join(cmd)}"
            ) from e


        move_and_rename_files(expected_out_dir, final_out_dir, keep_f_names)
        move_and_rename_files(expected_out_dir_maps, final_out_dir, keep_f_names_maps)


        # generate_messages(
        #     input_file=final_out_dir / f"blend_output_summary_{date_f}.csv",
        #     output_file=final_out_dir / f"kiremt_onset_categories_{date_f}.csv",
        # )

        if not debug:
            delete_all_files_in_dir(expected_out_dir)
            delete_all_files_in_dir(expected_out_dir_maps)


def main():
    parser = argparse.ArgumentParser(description="Run the Ethiopia 2026 pipeline.")
    parser.add_argument(
        "--date",
        type=str,
        help="The date for which to run the pipeline. Format: YYYYMMDDTHH",
    )

    parser.add_argument(
        "--ensemble_model",
        type=str,
        help="The ensemble model to blend.",
    )
    parser.add_argument(
        "--deterministic_model",
        type=str,
        help="The deterministic model to blend.",
    )
    parser.add_argument("--deterministic_input", type=str, default=None)
    parser.add_argument("--ensemble_input", type=str, default=None)
    parser.add_argument("--output_dir", type=str, default=None)

    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug mode.",
    )

    parser.add_argument(
        "--skip_to",
        type=int,
        default=None,
        help="Skip to a specific step in the pipeline for debugging.",
    )

    args = parser.parse_args()
    date_f = args.date
    ensemble_model = args.ensemble_model
    deterministic_model = args.deterministic_model
    debug = args.debug
    skip_to = args.skip_to
    with ethiopia_pipeline_lock():
        run_blending_pipeline(
            date_f,
            ensemble_model,
            deterministic_model,
            deterministic_input=args.deterministic_input,
            ensemble_input=args.ensemble_input,
            output_dir=args.output_dir,
            debug=debug,
            skip_to=skip_to,
        )


if __name__ == "__main__":
    main()
