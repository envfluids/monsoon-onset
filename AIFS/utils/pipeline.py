from download_ic import get_data
import os

def main():
    DATE_F = get_data()
    # DATE_F = "20250410T18"
    if DATE_F:
        print("Data downloaded and saved.")
        print(f"DATE: {DATE_F}")
        command = (
            f"sbatch "
            f"--job-name=AIFS_fc_{DATE_F} "
            f"--output=../logs/AIFS_fc_{DATE_F}.o%j "
            f"--error=../logs/AIFS_fc_{DATE_F}.e%j "
            f"--export=DATE_F={DATE_F} "
            f"run_model.sh"
        )

        os.system(command)
        print("Running model")

    else:
        print("Will not run model, no new data to download. Retrying in 15 minutes")

if __name__ == "__main__":
    main()