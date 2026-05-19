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
    log_name: str
    optional: bool = False


@dataclass(frozen=True)
class CompanionJobSpec:
    prep_path: Path
    prep_function: str
    job: JobSpec


@dataclass(frozen=True)
class PipelineSpec:
    name: str
    downloader_path: Path
    downloader_function: str
    allowed_hours: tuple[str, ...] = ()
    date_format: str = "datetime"


ROOT = Path(__file__).resolve().parents[2]
AIFS_GRIB_DIR = ROOT / "AIFS" / "raw" / "ifs_ic" / "grib"
GENCAST_SST_DIR = ROOT / "gencast" / "raw" / "sst_ic"

PIPELINES = {
    "aifs": {
        "pipeline": PipelineSpec(
            name="aifs",
            downloader_path=ROOT / "AIFS" / "utils" / "download_ic.py",
            downloader_function="get_data",
            allowed_hours=("00",),
        ),
        "jobs": [
            JobSpec(
                label="AIFS",
                script_name="run_AIFS.sh",
                work_dir=ROOT / "AIFS" / "utils",
                log_name="AIFS",
            )
        ],
    },
    "ngcm": {
        "pipeline": PipelineSpec(
            name="ngcm",
            downloader_path=ROOT / "NeuralGCM" / "utils" / "download_ncep.py",
            downloader_function="get_data",
            allowed_hours=("00",),
        ),
        "jobs": [
            JobSpec(
                label="NGCM",
                script_name="run_NGCM.sh",
                work_dir=ROOT / "NeuralGCM" / "utils",
                log_name="NGCM",
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
                log_name="IMERG",
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
                log_name="S2S",
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
        log_name="AIFS_ENS",
        optional=True,
    )
]

PIPELINES["ecmwf"] = {}
PIPELINES["ecmwf"]["pipeline"] = PIPELINES["aifs"]["pipeline"]
PIPELINES["ecmwf"]["jobs"] = []
GENCAST_COMPANION = CompanionJobSpec(
    prep_path=ROOT / "gencast" / "utils" / "download_sst.py",
    prep_function="get_sst",
    job=JobSpec(
        label="GenCast",
        script_name="run_gencast.sh",
        work_dir=ROOT / "gencast" / "utils",
        log_name="GenCast",
        optional=True,
    ),
)

PIPELINES["gencast"] = {
    "pipeline": PipelineSpec(
        name="gencast",
        downloader_path=PIPELINES["aifs"]["pipeline"].downloader_path,
        downloader_function=PIPELINES["aifs"]["pipeline"].downloader_function,
        allowed_hours=PIPELINES["aifs"]["pipeline"].allowed_hours,
    ),
    "jobs": [],
    "companions": [GENCAST_COMPANION],
}

PIPELINES["ecmwf"]["jobs"].extend(PIPELINES["aifs"]["jobs"])
PIPELINES["ecmwf"]["jobs"].extend(PIPELINES["aifs_ens"]["jobs"])
PIPELINES["ecmwf"]["companions"] = [GENCAST_COMPANION]


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


def expected_ecmwf_grib_paths(date_f):
    date = dt.datetime.strptime(date_f, "%Y%m%dT%H")
    grib_dates = [date - dt.timedelta(hours=12), date - dt.timedelta(hours=6), date]
    return [
        AIFS_GRIB_DIR / f"{grib_date.strftime('%Y%m%d%H')}0000-0h-oper-fc.grib2"
        for grib_date in grib_dates
    ]


def gencast_sst_path(date_f):
    return GENCAST_SST_DIR / f"sst_{date_f}.nc"


def get_latest_ecmwf_available_date():
    date = call_function(
        PIPELINES["aifs"]["pipeline"].downloader_path,
        "check_new_data",
    )
    if date is None:
        return None
    return date.strftime("%Y%m%dT%H")


def submit_missing_ecmwf_companions(date_f, dry_run=False):
    if not date_f:
        date_f = get_latest_ecmwf_available_date()
        if not date_f:
            logging.info("Will not check GenCast SST; no ECMWF date was found.")
            return False

    hour = date_f.split("T")[-1]
    if hour not in PIPELINES["aifs"]["pipeline"].allowed_hours:
        logging.info(
            "Will not check GenCast SST for %s; hour %s is not in allowed hours: %s",
            date_f,
            hour,
            PIPELINES["aifs"]["pipeline"].allowed_hours,
        )
        return False

    missing_gribs = [
        path for path in expected_ecmwf_grib_paths(date_f) if not path.exists()
    ]
    if missing_gribs:
        logging.info(
            "Will not run GenCast companion recovery for %s; missing ECMWF GRIBs: %s",
            date_f,
            ", ".join(str(path) for path in missing_gribs),
        )
        return False

    missing_companions = [
        companion
        for companion in PIPELINES["ecmwf"].get("companions", [])
        if companion.job.label == "GenCast" and not gencast_sst_path(date_f).exists()
    ]
    if not missing_companions:
        logging.info("No missing ECMWF companion inputs found for %s.", date_f)
        return False

    logging.info(
        "ECMWF GRIBs are complete for %s, but GenCast SST is missing at %s.",
        date_f,
        gencast_sst_path(date_f),
    )
    cluster_config = get_cluster()
    for companion in missing_companions:
        submit_companion_job(companion, cluster_config, date_f, dry_run=dry_run)
    return True


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

    command = build_command(
        cluster=cluster_config.name,
        label=job.label,
        script_path=script_path,
        log_dir=cluster_config.script_dir / "logs" / job.log_name,
        date_f=date_f,
    )
    if not dry_run:
        (cluster_config.script_dir / "logs" / job.log_name).mkdir(
            parents=True, exist_ok=True
        )
    return submit_job(
        command=command,
        cluster=cluster_config.name,
        label=job.label,
        cwd=job.work_dir,
        dry_run=dry_run,
    )


def submit_companion_job(companion, cluster_config, date_f, dry_run=False):
    job = companion.job
    script_path = cluster_config.script_dir / job.script_name
    if not script_path.exists():
        logging.info(
            "Skipping optional %s script missing on %s: %s",
            job.label,
            cluster_config.name,
            script_path,
        )
        return

    if dry_run:
        logging.info(
            "Dry run: would call %s:%s(%s)",
            companion.prep_path.relative_to(ROOT),
            companion.prep_function,
            date_f,
        )
    else:
        call_function(companion.prep_path, companion.prep_function, date_f)

    job_id = submit_job_spec(job, cluster_config, date_f, dry_run=dry_run)
    if not dry_run and not job.optional and job_id is None:
        raise RuntimeError(f"Required companion job submission failed: {job.label}")


def submit_pipeline_jobs(pipeline, date_f, dry_run=False):
    cluster_config = get_cluster()
    if pipeline == "imerg":
        run_imd_companion(date_f, dry_run=dry_run)

    for job in PIPELINES[pipeline]['jobs']:
        job_id = submit_job_spec(job, cluster_config, date_f, dry_run=dry_run)
        if not dry_run and not job.optional and job_id is None:
            raise RuntimeError(f"Required HPC job submission failed: {job.label}")

    for companion in PIPELINES[pipeline].get("companions", []):
        submit_companion_job(companion, cluster_config, date_f, dry_run=dry_run)


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

    if pipeline in {"ecmwf", "gencast"}:
        submit_missing_ecmwf_companions(resolved_date, dry_run=dry_run)


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
