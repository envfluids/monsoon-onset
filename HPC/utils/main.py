import argparse
import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from data_listener import call_function
from job_submitter import build_command, get_cluster, submit_job


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)


@dataclass(frozen=True)
class JobSpec:
    label: str
    script_name: str
    work_dir: Path
    models: tuple[str, ...] = ()
    optional: bool = False



@dataclass(frozen=True)
class PipelineSpec:
    name: str
    downloader_path: Path
    downloader_function: str
    allowed_hours: tuple[str, ...] = ()
    date_format: str = "datetime"


ROOT = Path(__file__).resolve().parents[2]
# AIFS_GRIB_DIR = ROOT / "AIFS" / "raw" / "ifs_ic" / "grib"
# GENCAST_SST_DIR = ROOT / "gencast" / "raw" / "sst_ic"

PIPELINES = {
    "aifs": {
        "pipeline": PipelineSpec(
            name="aifs",
            downloader_path=ROOT / "IC" / "utils" / "download_ecmwf.py",
            downloader_function="get_data",
            allowed_hours=("00",),
        ),
        "jobs": [
            JobSpec(
                label="AIFS",
                script_name="run_AIFS.sh",
                work_dir=ROOT / "AIFS" / "utils",
                models=("AIFS_single_v1p1",),
            )
        ],
    },
    "ngcm": {
        "pipeline": PipelineSpec(
            name="ngcm",
            downloader_path=ROOT / "IC" / "utils" / "download_ncep.py",
            downloader_function="get_data",
            allowed_hours=("00",),
        ),
        "jobs": [
            JobSpec(
                label="NGCM",
                script_name="run_NGCM.sh",
                work_dir=ROOT / "NeuralGCM" / "utils",
            )
        ],
    },
    "imerg": {
        "pipeline": PipelineSpec(
            name="imerg",
            downloader_path=ROOT / "IMERG" / "utils" / "download_imerg.py",
            downloader_function="get_data",
            date_format="date",
        ),
        "jobs": [
            JobSpec(
                label="IMERG",
                script_name="process_IMERG.sh",
                work_dir=ROOT / "IMERG" / "utils",
            )
        ],
    },
    "s2s": {
        "pipeline": PipelineSpec(
            name="s2s",
            downloader_path=ROOT / "S2S" / "utils" / "download_forecast.py",
            downloader_function="get_data",
            allowed_hours=("00", "12"),
        ),
        "jobs": [
            JobSpec(
                label="S2S",
                script_name="process_S2S.sh",
                work_dir=ROOT / "S2S" / "utils",
                optional=True,
            )
        ],
    },
}

PIPELINES["aifs_ens"] = {}
PIPELINES["aifs_ens"]["pipeline"] = PIPELINES["aifs"]["pipeline"]
PIPELINES["aifs_ens"]["jobs"] = [
    JobSpec(
        label="AIFS_ENS",
        script_name="run_AIFS_ENS.sh",
        work_dir=ROOT / "AIFS" / "utils",
        models=("AIFS_ENS_v1",),
        optional=True,
    )
]

PIPELINES["ecmwf"] = {}
PIPELINES["ecmwf"]["pipeline"] = PIPELINES["aifs"]["pipeline"]
PIPELINES["ecmwf"]["jobs"] = []

PIPELINES["gencast"] = {
    "pipeline": PipelineSpec(
        name="gencast",
        downloader_path=PIPELINES["aifs"]["pipeline"].downloader_path,
        downloader_function=PIPELINES["aifs"]["pipeline"].downloader_function,
        allowed_hours=PIPELINES["aifs"]["pipeline"].allowed_hours,
    ),
    "jobs": [
        JobSpec(
            label="GenCast",
            script_name="run_gencast.sh",
            work_dir=ROOT / "gencast" / "utils",
            optional=True,
        )
    ]
}

PIPELINES["ecmwf"]["jobs"].extend(PIPELINES["aifs"]["jobs"])
PIPELINES["ecmwf"]["jobs"].extend(PIPELINES["aifs_ens"]["jobs"])
PIPELINES["ecmwf"]["jobs"].extend(PIPELINES["gencast"]["jobs"])


def normalize_date(value, pipeline):
    if value is None:
        return None

    value = value.strip()
    if pipeline == "imerg":
        if len(value) == 8:
            return value
        if len(value) == 11 and "T" in value:
            return value.split("T", 1)[0]
        if len(value) == 10:
            return value[:8]
        raise ValueError(
            f"IMERG dates must be YYYYMMDD, YYYYMMDDHH, or YYYYMMDDTHH: {value}"
        )

    if len(value) == 8:
        return f"{value}T00"
    if len(value) == 10:
        return f"{value[:8]}T{value[8:]}"
    if len(value) == 11 and "T" in value:
        return value
    raise ValueError(f"Dates must be YYYYMMDD, YYYYMMDDHH, or YYYYMMDDTHH: {value}")


def iter_date_range(start_date, end_date):
    start = dt.datetime.strptime(start_date, "%Y%m%d")
    end = dt.datetime.strptime(end_date or start_date, "%Y%m%d")
    if end < start:
        raise ValueError("--end-date must be on or after --start-date")
    for offset in range((end - start).days + 1):
        yield (start + dt.timedelta(days=offset)).strftime("%Y%m%dT00")


def get_latest_date(spec):
    if spec.name == "s2s":
        return call_function(spec.downloader_path, spec.downloader_function, None)
    return call_function(spec.downloader_path, spec.downloader_function)


def get_latest_ecmwf_available_date():
    date = call_function(
        PIPELINES["aifs"]["pipeline"].downloader_path,
        "check_new_data",
    )
    if date is None:
        return None
    return date.strftime("%Y%m%dT%H")


def run_imd_companion(date_f, dry_run=False):
    if dry_run:
        logging.info(
            "Dry run: would call IMERG/utils/download_imd.py:get_imd_data(%s)", date_f
        )
        return
    call_function(
        ROOT / "IMERG" / "utils" / "download_imd.py",
        "get_imd_data",
        date_str=date_f,
    )


def submit_job_spec(job, cluster_config, date_f, dry_run=False):
    script_path = cluster_config.script_dir / job.script_name
    if not script_path.exists():
        if job.optional:
            logging.info(
                "Skipping optional %s script missing on %s: %s",
                job.label,
                cluster_config.name,
                script_path,
            )
            return None
        raise FileNotFoundError(f"Required HPC script is missing: {script_path}")

    submitted_job_id = None
    for label in job.models or (job.label,):

        command = build_command(
            cluster=cluster_config.name,
            label=label,
            script_path=script_path,
            log_dir=cluster_config.script_dir / "logs" / label,
            date_f=date_f,
        )
        if not dry_run:
            (cluster_config.script_dir / "logs" / label).mkdir(
                parents=True, exist_ok=True
            )
        submitted_job_id = submit_job(
            command=command,
            cluster=cluster_config.name,
            label=label,
            cwd=job.work_dir,
            dry_run=dry_run,
        )
        if not dry_run and not job.optional and submitted_job_id is None:
            raise RuntimeError(f"Required HPC job submission failed: {label}")

    return submitted_job_id


def submit_pipeline_jobs(pipeline, date_f, dry_run=False):
    cluster_config = get_cluster()
    if pipeline == "imerg":
        run_imd_companion(date_f, dry_run=dry_run)

    for job in PIPELINES[pipeline]['jobs']:
        job_id = submit_job_spec(job, cluster_config, date_f, dry_run=dry_run)
        if not dry_run and not job.optional and job_id is None:
            raise RuntimeError(f"Required HPC job submission failed: {job.label}")


def should_submit(spec, date_f, explicit_date):
    if not date_f:
        logging.info("Will not submit %s; no new data was found.", spec.name)
        return False

    if spec.allowed_hours:
        hour = date_f.split("T")[-1]
        if hour not in spec.allowed_hours:
            logging.info(
                "New data available for %s, but hour %s is not in allowed hours: %s",
                date_f,
                hour,
                spec.allowed_hours,
            )
            return False

    if spec.name == "s2s" and not explicit_date:
        output_path = ROOT / "S2S" / "output" / "india" /date_f
        if output_path.exists():
            logging.info("Output path %s already exists. Exiting.", output_path)
            return False

    return True


def run_one(pipeline, date_f=None, dry_run=False):
    spec = PIPELINES[pipeline]['pipeline']
    explicit_date = date_f is not None
    if explicit_date:
        resolved_date = normalize_date(date_f, pipeline)
        logging.info("Using explicit date for %s: %s", pipeline, resolved_date)
    else:
        logging.info(
            "No date provided for %s; checking latest available data.", pipeline
        )
        resolved_date = get_latest_date(spec)

    if should_submit(spec, resolved_date, explicit_date):
        submit_pipeline_jobs(pipeline, resolved_date, dry_run=dry_run)
        return


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run centralized HPC pipeline orchestration."
    )
    parser.add_argument(
        "--pipelines", nargs="+", required=True, choices=sorted(PIPELINES)
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Single date: YYYYMMDD, YYYYMMDDHH, or YYYYMMDDTHH.",
    )
    parser.add_argument(
        "--start-date", type=str, default=None, help="S2S date range start: YYYYMMDD."
    )
    parser.add_argument(
        "--end-date", type=str, default=None, help="S2S date range end: YYYYMMDD."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve work and print submission commands without submitting.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.date and args.start_date:
        raise ValueError("Use either --date or --start-date/--end-date, not both.")
    if args.start_date and args.pipelines != ["s2s"]:
        raise ValueError(
            "--start-date/--end-date is only supported with --pipelines s2s."
        )

    if args.start_date:
        for date_f in iter_date_range(args.start_date, args.end_date):
            run_one("s2s", date_f=date_f, dry_run=args.dry_run)
        return

    for pipeline in args.pipelines:
        run_one(pipeline, date_f=args.date, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
