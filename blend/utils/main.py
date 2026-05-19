import argparse
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - %(pathname)s:%(lineno)d - %(message)s"
    ),
)
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATE_RE = re.compile(r"^\d{8}T\d{2}$")


@dataclass(frozen=True)
class ForecastInput:
    model: str
    role: str
    path_template: str

    def path(self, date: str) -> Path:
        return REPO_ROOT / self.path_template.format(date=date)


@dataclass(frozen=True)
class BlendConfig:
    region: str
    name: str
    deterministic_model: str
    ensemble_model: str
    script: Path
    inputs: tuple[ForecastInput, ...]
    output_dir_template: str

    def models(self) -> set[str]:
        return {self.deterministic_model, self.ensemble_model}

    def input_paths(self, date: str) -> dict[str, Path]:
        return {input_.model: input_.path(date) for input_ in self.inputs}

    def output_dir(self, date: str) -> Path:
        return REPO_ROOT / self.output_dir_template.format(date=date)

    def command(
        self,
        date: str,
        debug: bool = False,
        skip_to: int | None = None,
    ) -> list[str]:
        cmd = [sys.executable, str(self.script), "--date", date]

        if self.region == "ethiopia":
            cmd.extend(
                [
                    "--ensemble_model",
                    self.ensemble_model,
                    "--deterministic_model",
                    self.deterministic_model,
                ]
            )
            if debug:
                cmd.append("--debug")
            if skip_to is not None:
                cmd.extend(["--skip_to", str(skip_to)])

        return cmd


BLENDS: tuple[BlendConfig, ...] = (
    BlendConfig(
        region="ethiopia",
        name="AIFS_AIFS_ENS",
        deterministic_model="AIFS",
        ensemble_model="AIFS_ENS",
        script=REPO_ROOT / "blend" / "utils" / "ethiopia2026" / "run_pipeline.py",
        inputs=(
            ForecastInput(
                model="AIFS",
                role="deterministic",
                path_template="AIFS/output/ethiopia/AIFS/tp/tp_0p25_{date}.nc",
            ),
            ForecastInput(
                model="AIFS_ENS",
                role="ensemble",
                path_template="AIFS/output/ethiopia/AIFS_ENS/tp/tp_0p25_{date}.nc",
            ),
        ),
        output_dir_template="blend/output/ethiopia2026/{date}",
    ),
    BlendConfig(
        region="india",
        name="AIFS_NCUM",
        deterministic_model="AIFS",
        ensemble_model="NCUM",
        script=REPO_ROOT
        / "blend"
        / "utils"
        / "india2026"
        / "AIFS_NCUM_blend"
        / "main.py",
        inputs=(
            ForecastInput(
                model="AIFS",
                role="deterministic",
                path_template="AIFS/output/india/tp/tp_0p25_{date}.nc",
            ),
            ForecastInput(
                model="NCUM",
                role="ensemble",
                path_template="NCUM/output/precipitation_amount/precipitation_amount_{date}.nc",
            ),
        ),
        output_dir_template="blend/output/india2026/{date}/AIFS_NCUM",
    ),
)


def normalize_model(model: str | None) -> str | None:
    return model.upper() if model else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run any blend whose same-date deterministic and ensemble forecast "
            "inputs are available."
        )
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Forecast initialization date in YYYYMMDDTHH format.",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="Limit orchestration to one region, such as india or ethiopia.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model that just became available. Only blends using this model are checked.",
    )
    parser.add_argument(
        "--blend",
        default=None,
        help="Limit orchestration to one configured blend name, such as AIFS_NCUM.",
    )
    parser.add_argument(
        "--ensemble_model",
        default=None,
        help="Optional legacy filter for the ensemble model in a blend.",
    )
    parser.add_argument(
        "--deterministic_model",
        default=None,
        help="Optional legacy filter for the deterministic model in a blend.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log eligible blend commands without executing them.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Forward debug mode to blend scripts that support it.",
    )
    parser.add_argument(
        "--skip_to",
        type=int,
        default=None,
        help="Forward skip_to to blend scripts that support it.",
    )
    return parser.parse_args()


def validate_date(date: str) -> None:
    if not DATE_RE.match(date):
        raise ValueError(f"Invalid --date {date!r}; expected YYYYMMDDTHH.")


def validate_blend_config(blend: BlendConfig) -> None:
    roles = {input_.role for input_ in blend.inputs}
    if roles != {"deterministic", "ensemble"} or len(blend.inputs) != 2:
        raise ValueError(
            f"Blend {blend.name} must define exactly one deterministic and one ensemble input."
        )
    if {input_.model for input_ in blend.inputs} != blend.models():
        raise ValueError(f"Blend {blend.name} model names do not match its inputs.")


def select_blends(args: argparse.Namespace) -> list[BlendConfig]:
    model = normalize_model(args.model)
    ensemble_model = normalize_model(args.ensemble_model)
    deterministic_model = normalize_model(args.deterministic_model)
    region = args.region.lower() if args.region else None
    blend_name = args.blend.upper() if args.blend else None

    selected = []
    for blend in BLENDS:
        validate_blend_config(blend)
        if region and blend.region != region:
            continue
        if blend_name and blend.name.upper() != blend_name:
            continue
        if model and model not in blend.models():
            continue
        if ensemble_model and blend.ensemble_model != ensemble_model:
            continue
        if deterministic_model and blend.deterministic_model != deterministic_model:
            continue
        selected.append(blend)

    return selected


def missing_inputs(blend: BlendConfig, date: str) -> dict[str, Path]:
    return {
        model: path
        for model, path in blend.input_paths(date).items()
        if not path.exists()
    }


def run_blend(
    blend: BlendConfig,
    date: str,
    dry_run: bool,
    debug: bool,
    skip_to: int | None,
) -> bool:
    missing = missing_inputs(blend, date)
    if missing:
        logger.info(
            "Blend %s/%s is not ready for %s. Missing: %s",
            blend.region,
            blend.name,
            date,
            ", ".join(f"{model}={path}" for model, path in missing.items()),
        )
        return False

    output_dir = blend.output_dir(date)
    if output_dir.exists():
        logger.info(
            "Blend %s/%s already has output at %s; rerunning with %s + %s.",
            blend.region,
            blend.name,
            output_dir,
            blend.deterministic_model,
            blend.ensemble_model,
        )
    else:
        logger.info(
            "Blend %s/%s is ready for %s with %s + %s.",
            blend.region,
            blend.name,
            date,
            blend.deterministic_model,
            blend.ensemble_model,
        )

    cmd = blend.command(date, debug=debug, skip_to=skip_to)
    logger.info("Blend command: %s", " ".join(cmd))
    if dry_run:
        return True

    subprocess.run(cmd, check=True, cwd=blend.script.parent)
    return True


def main() -> None:
    args = parse_args()
    validate_date(args.date)

    selected = select_blends(args)
    if not selected:
        logger.info("No configured blends matched the supplied filters.")
        return

    ran_any = False
    for blend in selected:
        ran_any = (
            run_blend(
                blend,
                args.date,
                dry_run=args.dry_run,
                debug=args.debug,
                skip_to=args.skip_to,
            )
            or ran_any
        )

    if not ran_any:
        logger.info("No blends were run for %s.", args.date)


if __name__ == "__main__":
    main()
