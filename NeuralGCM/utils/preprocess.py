import os
import subprocess
import xarray as xr
import datetime
import argparse
import logging # Already imported, good.

# Configure logging - This setup is good.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def run_ncl(in_file, out_file_interp):
    """
    Runs the NCL interpolation script.

    Args:
        in_file (str): Path to the input GRIB2 file.
        out_file_interp (str): Path for the intermediate NetCDF output file.

    Returns:
        bool: True if the NCL script executed successfully, False otherwise.
    """
    logging.info(f"Starting NCL interpolation for {in_file}")

    # --- Check if input file exists ---
    if not os.path.exists(in_file):
        logging.error(f"Input file for NCL not found: {in_file}")
        return False

    # --- NCL Script Content ---
    # DO NOT MODIFY THIS NCL SCRIPT CONTENT
    ncl_script_content = f"""
    begin
    f = addfile("{in_file}", "r")
    T  = f->TMP_P0_L100_GLL0
    ; RH = f->RH_P0_L100_GLL0
    S  = f->SPFH_P0_L100_GLL0
    U  = f->UGRD_P0_L100_GLL0
    V  = f->VGRD_P0_L100_GLL0
    HGT = f->HGT_P0_L100_GLL0
    p_Var = f->lv_ISBL0
    p_CW = f->lv_ISBL6
    CW = f->CLWMR_P0_L100_GLL0
    new_p = (/ 100, 200, 300, 500, 700, 1000, 2000, 3000, 5000, 7000, 10000, 12500, \
                15000, 17500, 20000, 22500, 25000, 30000, 35000, 40000, 45000, 50000, \
                55000, 60000, 65000, 70000, 75000, 77500, 80000, 82500, 85000, \
                87500, 90000, 92500, 95000, 97500, 100000 /)

    linlog = 2
    T_interp = int2p_n(p_Var, T, new_p, linlog, 0)
    ; RH_interp = int2p_n(p_Var, RH, new_p, linlog, 0)
    S_interp = int2p_n(p_Var, S, new_p, linlog, 0)
    U_interp = int2p_n(p_Var, U, new_p, linlog, 0)
    V_interp = int2p_n(p_Var, V, new_p, linlog, 0)
    HGT_interp = int2p_n(p_Var, HGT, new_p, linlog, 0)
    CW_interp = int2p_n(p_CW, CW, new_p, linlog, 0)

    ; Save to NetCDF directly in timed_files
    fout = addfile("{out_file_interp}", "c")
    fout->T_interp = T_interp
    ; fout->RH_interp = RH_interp
    fout->S_interp = S_interp
    fout->U_interp = U_interp
    fout->V_interp = V_interp
    fout->HGT_interp = HGT_interp
    fout->CW_interp = CW_interp
    end
    """

    logging.info("Running NCL script via stdin...")
    try:
        process = subprocess.run(
            ['ncl'],
            input=ncl_script_content,
            capture_output=True,
            text=True,
            check=True, # Raises CalledProcessError if return code is non-zero
            encoding='utf-8' # Explicitly set encoding
        )
        logging.info("NCL script executed successfully.")
        logging.debug("--- NCL Stdout ---:\n%s", process.stdout)
        # NCL often prints warnings/info to stderr even on success
        if process.stderr:
             logging.debug("--- NCL Stderr ---:\n%s", process.stderr)
        # --- Check if output file was created ---
        if not os.path.exists(out_file_interp):
            logging.error(f"NCL ran but the output file was not created: {out_file_interp}")
            return False
        return True # Success
    except subprocess.CalledProcessError as e:
        logging.error(f"Error running NCL script (return code {e.returncode}).")
        logging.error("--- NCL Stdout ---:\n%s", e.stdout)
        logging.error("--- NCL Stderr ---:\n%s", e.stderr)
        # Use logging.exception to include traceback info automatically
        logging.exception("NCL subprocess execution failed.")
        return False # Failure
    except FileNotFoundError:
        logging.error("Error: 'ncl' command not found. Is NCL installed and in your system's PATH?")
        return False # Failure
    except Exception as e:
        # Catch any other unexpected errors during subprocess execution
        logging.exception(f"An unexpected error occurred during NCL execution: {e}")
        return False # Failure

def make_ds(out_file_interp, out_file_ic, date_f):
    """
    Processes the intermediate NCL output and creates the final initial conditions file.

    Args:
        out_file_interp (str): Path to the intermediate NetCDF file from NCL.
        out_file_ic (str): Path for the final NetCDF output file.
        date_f (str): Date string used for the time dimension.

    Returns:
        bool: True if processing was successful, False otherwise.
    """
    logging.info(f"Starting dataset processing for {out_file_interp}")
    ds_ncep = None # Initialize to None
    ds_ERA = None  # Initialize to None

    try:
        # --- Load Reference Dataset ---
        path_ERA = '../data/model_ds/'
        era_file = os.path.join(path_ERA, "ERA5_2018_05_16_00.nc") # Use a specific file
        logging.info(f"Loading reference ERA5 dataset: {era_file}")
        if not os.path.exists(era_file):
            logging.error(f"Reference ERA5 file not found: {era_file}")
            return False
        ds_ERA = xr.open_dataset(era_file)
        levels = ds_ERA['level'].values
        lats = ds_ERA['latitude'].values
        lons = ds_ERA['longitude'].values
        logging.info("Reference dataset loaded successfully.")

        # --- Load NCL Output Dataset ---
        logging.info(f"Loading intermediate NCL output dataset: {out_file_interp}")
        if not os.path.exists(out_file_interp):
            logging.error(f"Intermediate NCL output file not found: {out_file_interp}")
            return False
        ds_ncep = xr.open_dataset(out_file_interp)
        logging.info("Intermediate dataset loaded successfully.")

        # --- Process Date ---
        try:
            DATE = datetime.datetime.strptime(date_f, "%Y%m%dT%H")
        except ValueError:
            logging.error(f"Invalid date format: {date_f}. Expected YYYYMMDDTHH.")
            return False

        # --- Define Renaming Maps ---
        RENAME_DIMS = {
            'ncl0': 'level', 'ncl1': 'latitude', 'ncl2': 'longitude',
            'ncl3': 'level', 'ncl4': 'latitude', 'ncl5': 'longitude',
            'ncl6': 'level', 'ncl7': 'latitude', 'ncl8': 'longitude',
            'ncl9': 'level', 'ncl10': 'latitude', 'ncl11': 'longitude',
            'ncl12': 'level', 'ncl13': 'latitude', 'ncl14': 'longitude',
            'ncl15': 'level', 'ncl16': 'latitude', 'ncl17': 'longitude',
        }
        RENAME_VARS = {"T_interp": "temperature",
                       "U_interp": "u_component_of_wind",
                       "V_interp": "v_component_of_wind",
                       "S_interp": "specific_humidity",
                       "HGT_interp": "geopotential"}

        # --- Nested Processing Function ---
        def process(ds_to_process, target_levels, target_lats, target_lons, time_coord):
            logging.debug("Starting internal processing function.")
            # Check if 'time' dimension already exists before expanding
            if 'time' not in ds_to_process.dims:
                 ds_to_process = ds_to_process.expand_dims('time')
            ds_to_process['time'] = [time_coord]

            # Safely rename dimensions
            rename_safe_dims = {k: v for k, v in RENAME_DIMS.items() if k in ds_to_process.dims}
            if rename_safe_dims:
                logging.debug(f"Renaming dimensions: {rename_safe_dims}")
                ds_to_process = ds_to_process.rename(rename_safe_dims)
            else:
                logging.warning("No dimensions found matching the RENAME_DIMS pattern.")

            # Safely rename variables
            rename_safe_vars = {k: v for k, v in RENAME_VARS.items() if k in ds_to_process.data_vars}
            if rename_safe_vars:
                logging.debug(f"Renaming variables: {rename_safe_vars}")
                ds_to_process = ds_to_process.rename(rename_safe_vars)
            else:
                 logging.warning("No variables found matching the RENAME_VARS pattern.")

            # Assign coordinates - potential source of errors if shapes mismatch
            logging.debug("Assigning coordinates (level, lat, lon).")
            ds_to_process = ds_to_process.assign_coords(level=target_levels, latitude=target_lats, longitude=target_lons)
            ds_to_process['geopotential'] = ds_to_process['geopotential'] * 9.81
            # Calculate cloud water/ice content
            if 'CW_interp' in ds_to_process:
                logging.debug("Calculating cloud liquid/ice water content.")
                CWMR = ds_to_process['CW_interp']     # Cloud water mixing ratio
                T_K = ds_to_process['temperature']    # Temperature in Kelvin
                T_C = T_K - 273.15                    # Convert to Celsius

                liquid_condition = T_C >= -20
                ice_condition = T_C < -20

                CLWMR = CWMR.where(liquid_condition, 0.0).fillna(0.0)
                CIWMR = CWMR.where(ice_condition, 0.0).fillna(0.0)

                ds_to_process['specific_cloud_liquid_water_content'] = CLWMR
                ds_to_process['specific_cloud_ice_water_content'] = CIWMR

                logging.debug("Dropping intermediate 'CW_interp' variable.")
                ds_to_process = ds_to_process.drop_vars("CW_interp")
            else:
                logging.warning("Variable 'CW_interp' not found. Skipping cloud water calculation.")


            logging.debug("Internal processing function finished.")
            return ds_to_process

        # --- Apply Processing ---
        logging.info("Applying processing steps to the dataset.")
        ds_processed = process(ds_ncep, levels, lats, lons, DATE)

        # --- Save Final Dataset ---
        logging.info(f"Saving final processed dataset to: {out_file_ic}")
        ds_processed.to_netcdf(out_file_ic)
        logging.info("Final dataset saved successfully.")

        return True # Success

    except FileNotFoundError as e:
        # This might be caught earlier, but good to have redundancy
        logging.error(f"File not found during dataset processing: {e}")
        return False
    except xr.backends.opener.DatasetBuildError as e:
        logging.error(f"Error opening NetCDF file (possibly corrupt or invalid format): {e}")
        logging.exception("Dataset opening failed.")
        return False
    except KeyError as e:
        logging.error(f"Missing expected variable or coordinate in dataset: {e}")
        logging.exception("Dataset processing failed due to missing key.")
        return False
    except ValueError as e:
        logging.error(f"Data value or dimension mismatch during processing: {e}")
        logging.exception("Dataset processing failed due to value error.")
        return False
    except (IOError, OSError) as e:
        logging.error(f"Error writing final NetCDF file to {out_file_ic}: {e}")
        logging.exception("File writing failed.")
        return False
    except Exception as e:
        # Catch-all for any other unexpected errors during processing
        logging.exception(f"An unexpected error occurred during dataset processing: {e}")
        return False
    finally:
        # --- Ensure datasets are closed ---
        if ds_ncep is not None:
            ds_ncep.close()
            logging.debug("Closed intermediate dataset.")
        if ds_ERA is not None:
            ds_ERA.close()
            logging.debug("Closed reference ERA5 dataset.")


def main():
    """Main function to orchestrate the data processing workflow."""
    logging.info("Starting main processing script.")
    parser = argparse.ArgumentParser(
        description="Process NCEP GDAS data: Interpolate using NCL and format using Python."
    )
    parser.add_argument(
        "--date",
        type=str,
        required=True, # Make date mandatory
        help="Date for the processing in YYYYMMDDTHH format (e.g., 20030625T06)",
    )

    args = parser.parse_args()
    date_f = args.date
    logging.info(f"Processing data for date: {date_f}")

    # Define directories and file paths
    base_dir = "../raw/ncep_ic" # Define base directory for clarity
    input_dir = os.path.join(base_dir, "download")
    output_dir = os.path.join(base_dir, "processed")

    # Create output directory if it doesn't exist
    try:
        os.makedirs(output_dir, exist_ok=True)
        logging.info(f"Ensured output directory exists: {output_dir}")
    except OSError as e:
        logging.error(f"Could not create output directory {output_dir}: {e}")
        return # Cannot proceed without output directory

    in_file = os.path.join(input_dir, f"gdas_{date_f}.pgrb2")
    out_file_interp = os.path.join(output_dir, f"INTERMEDIATE_gdas_{date_f}.nc")
    out_file_ic = os.path.join(output_dir, f"gdas_{date_f}.nc")

    logging.info(f"Input file: {in_file}")
    logging.info(f"Intermediate output file: {out_file_interp}")
    logging.info(f"Final output file: {out_file_ic}")

    # --- Step 1: Run NCL Interpolation ---
    ncl_success = run_ncl(in_file, out_file_interp)

    if not ncl_success:
        logging.error("NCL processing failed. Aborting.")
        return # Stop execution if NCL failed

    logging.info("NCL processing completed successfully.")

    # --- Step 2: Process NCL Output with Python/Xarray ---
    make_ds_success = make_ds(out_file_interp, out_file_ic, date_f)

    if not make_ds_success:
        logging.error("Dataset processing (make_ds) failed. Aborting.")
        # Keep intermediate file for debugging if make_ds failed
        logging.warning(f"Intermediate file {out_file_interp} may still exist for debugging.")
        return # Stop execution if make_ds failed

    logging.info("Dataset processing completed successfully.")

    # --- Step 3: Clean up intermediate file ---
    logging.info(f"Attempting to remove intermediate file: {out_file_interp}")
    try:
        if os.path.exists(out_file_interp):
            os.remove(out_file_interp)
            logging.info(f"Successfully removed intermediate file: {out_file_interp}")
        else:
            logging.warning(f"Intermediate file not found, cannot remove: {out_file_interp}")
    except OSError as e:
        # Log as a warning because the main task is done, but cleanup failed.
        logging.warning(f"Error removing intermediate file {out_file_interp}: {e}")

    logging.info("Script finished successfully.")


if __name__ == "__main__":
    main()