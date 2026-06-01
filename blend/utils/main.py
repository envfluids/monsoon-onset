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
    diagnostic_inputs: tuple[ForecastInput, ...] | None = None
    implemented: bool = True
    diagnostic_plots: bool = False
    diagnostic_output_dir_template: str | None = None

    def models(self) -> set[str]:
        return {self.deterministic_model, self.ensemble_model}

    def input_paths(self, date: str) -> dict[str, Path]:
        return {input_.model: input_.path(date) for input_ in self.inputs}

    def diagnostic_input_paths(self, date: str) -> dict[str, Path]:
        inputs = self.diagnostic_inputs or self.inputs
        return {input_.model: input_.path(date) for input_ in inputs}

    def output_dir(self, date: str) -> Path:
        return REPO_ROOT / self.output_dir_template.format(date=date)

    def diagnostic_output_dir(self, date: str) -> Path:
        template = (
            self.diagnostic_output_dir_template
            or f"model_diagnostics/output/{self.region}/{{date}}/{self.name}"
        )
        return REPO_ROOT / template.format(date=date)

    def input_for_role(self, role: str) -> ForecastInput:
        for input_ in self.inputs:
            if input_.role == role:
                return input_
        raise ValueError(f"Blend {self.name} has no {role!r} input.")

    def diagnostic_input_for_role(self, role: str) -> ForecastInput:
        inputs = self.diagnostic_inputs or self.inputs
        for input_ in inputs:
            if input_.role == role:
                return input_
        raise ValueError(f"Blend {self.name} has no {role!r} diagnostic input.")

    def command(
        self,
        date: str,
        debug: bool = False,
        skip_to: int | None = None,
    ) -> list[str]:
        deterministic_input = self.input_for_role("deterministic").path(date)
        ensemble_input = self.input_for_role("ensemble").path(date)
        cmd = [sys.executable, str(self.script), "--date", date]
        cmd.extend(
            [
                "--deterministic_model",
                self.deterministic_model,
                "--ensemble_model",
                self.ensemble_model,
                "--deterministic_input",
                str(deterministic_input),
                "--ensemble_input",
                str(ensemble_input),
                "--output_dir",
                str(self.output_dir(date)),
            ]
        )
        if debug:
            cmd.append("--debug")
        if skip_to is not None:
            cmd.extend(["--skip_to", str(skip_to)])

        return cmd

    def diagnostics_command(self, date: str) -> list[str]:
        deterministic_input = self.diagnostic_input_for_role("deterministic").path(date)
        ensemble_input = self.diagnostic_input_for_role("ensemble").path(date)
        return [
            sys.executable,
            str(REPO_ROOT / "model_diagnostics" / "utils" / "main.py"),
            "--date",
            date,
            "--region",
            self.region,
            "--deterministic_model",
            self.deterministic_model,
            "--ensemble_model",
            self.ensemble_model,
            "--deterministic_input",
            str(deterministic_input),
            "--ensemble_input",
            str(ensemble_input),
            "--output_dir",
            str(self.diagnostic_output_dir(date)),
        ]


BLENDS: tuple[BlendConfig, ...] = (
    BlendConfig(
        region="ethiopia",
        name="AIFS_single_v1p1_AIFS_ENS_v1",
        deterministic_model="AIFS_single_v1p1",
        ensemble_model="AIFS_ENS_v1",
        script=REPO_ROOT / "blend" / "utils" / "ethiopia2026" / "run_pipeline.py",
        inputs=(
            ForecastInput(
                model="AIFS_single_v1p1",
                role="deterministic",
                path_template="AIFS/output/ethiopia/AIFS_single_v1p1/tp/tp_0p25_{date}.nc",
            ),
            ForecastInput(
                model="AIFS_ENS_v1",
                role="ensemble",
                path_template="AIFS/output/ethiopia/AIFS_ENS_v1/tp/tp_0p25_{date}.nc",
            ),
        ),
        output_dir_template="blend/output/ethiopia2026/{date}/AIFS_single_v1p1_AIFS_ENS_v1",
        implemented=True,
        diagnostic_plots=True,
    ),
    BlendConfig(
        region="ethiopia",
        name="AIFS_single_v2_AIFS_ENS_v2",
        deterministic_model="AIFS_single_v2",
        ensemble_model="AIFS_ENS_v2",
        script=REPO_ROOT / "blend" / "utils" / "ethiopia2026" / "run_pipeline.py",
        inputs=(
            ForecastInput(
                model="AIFS_single_v2",
                role="deterministic",
                path_template="AIFS/output/ethiopia/AIFS_single_v2/tp/tp_0p25_{date}.nc",
            ),
            ForecastInput(
                model="AIFS_ENS_v2",
                role="ensemble",
                path_template="AIFS/output/ethiopia/AIFS_ENS_v2/tp/tp_0p25_{date}.nc",
            ),
        ),
        output_dir_template="blend/output/ethiopia2026/{date}/AIFS_single_v2_AIFS_ENS_v2",
        # Diagnostics-only: no v2 blend coefficients are available.
        implemented=True,
        diagnostic_plots=True,
    ),
    BlendConfig(
        region="ethiopia",
        name="AIFS_single_v2_NeuralGCM",
        deterministic_model="AIFS_single_v2",
        ensemble_model="NeuralGCM",
        script=REPO_ROOT / "blend" / "utils" / "ethiopia2026" / "run_pipeline.py",
        inputs=(
            ForecastInput(
                model="AIFS_single_v2",
                role="deterministic",
                path_template="AIFS/output/ethiopia/AIFS_single_v2/tp/tp_0p25_{date}.nc",
            ),
            ForecastInput(
                model="NeuralGCM",
                role="ensemble",
                path_template="NeuralGCM/output/ethiopia/tp/tp_2p8_{date}.nc",
            ),
        ),
        output_dir_template="blend/output/ethiopia2026/{date}/AIFS_single_v2_NeuralGCM",
        # Diagnostics-only: no v2 blend coefficients are available.
        implemented=True,
        diagnostic_plots=False,
    ),
    BlendConfig(
        region="india",
        name="AIFS_single_v1p1_NCUM",
        deterministic_model="AIFS_single_v1p1",
        ensemble_model="NCUM",
        script=REPO_ROOT
        / "blend"
        / "utils"
        / "india2026"
        / "AIFS_NCUM_blend"
        / "main.py",
        inputs=(
            ForecastInput(
                model="AIFS_single_v1p1",
                role="deterministic",
                path_template="AIFS/output/india/AIFS_single_v1p1/tp/tp_0p25_{date}.nc",
            ),
            ForecastInput(
                model="NCUM",
                role="ensemble",
                path_template="NCUM/output/precipitation_amount/precipitation_amount_{date}.nc",
            ),
        ),
        output_dir_template="blend/output/india2026/{date}/AIFS_single_v1p1_NCUM",
        diagnostic_plots=False,
    ),
    BlendConfig(
        region="india",
        name="AIFS_single_v1p1_NeuralGCM",
        deterministic_model="AIFS_single_v1p1",
        ensemble_model="NeuralGCM",
        script=REPO_ROOT
        / "blend"
        / "utils"
        / "india2026"
        / "AIFS_NGCM_blend"
        / "main.py",
        inputs=(
            ForecastInput(
                model="AIFS_single_v1p1",
                role="deterministic",
                path_template="AIFS/output/india/AIFS_single_v1p1/tp/tp_0p25_{date}.nc",
            ),
            ForecastInput(
                model="NeuralGCM",
                role="ensemble",
                path_template="NeuralGCM/output/india/tp/tp_2p0_{date}.nc",
            ),
        ),
        output_dir_template="blend/output/india2026/{date}/AIFS_single_v1p1_NeuralGCM",
        diagnostic_inputs=(
            ForecastInput(
                model="AIFS_single_v1p1",
                role="deterministic",
                path_template="AIFS/output/india/AIFS_single_v1p1/tp/tp_2p0_{date}.nc",
            ),
            ForecastInput(
                model="NeuralGCM",
                role="ensemble",
                path_template="NeuralGCM/output/india/tp/tp_2p0_{date}.nc",
            ),
        ),
        diagnostic_plots=True,
    ),
    BlendConfig(
        region="india",
        name="AIFS_single_v2_NeuralGCM",
        deterministic_model="AIFS_single_v2",
        ensemble_model="NeuralGCM",
        script=REPO_ROOT
        / "blend"
        / "utils"
        / "india2026"
        / "AIFS_NGCM_blend"
        / "main.py",
        inputs=(
            ForecastInput(
                model="AIFS_single_v2",
                role="deterministic",
                path_template="AIFS/output/india/AIFS_single_v2/tp/tp_0p25_{date}.nc",
            ),
            ForecastInput(
                model="NeuralGCM",
                role="ensemble",
                path_template="NeuralGCM/output/india/tp/tp_2p0_{date}.nc",
            ),
        ),
        output_dir_template="blend/output/india2026/{date}/AIFS_single_v2_NeuralGCM",
        diagnostic_inputs=(
            ForecastInput(
                model="AIFS_single_v2",
                role="deterministic",
                path_template="AIFS/output/india/AIFS_single_v2/tp/tp_2p0_{date}.nc",
            ),
            ForecastInput(
                model="NeuralGCM",
                role="ensemble",
                path_template="NeuralGCM/output/india/tp/tp_2p0_{date}.nc",
            ),
        ),
        # Diagnostics-only: no v2 blend coefficients are available.
        implemented=False,
        diagnostic_plots=True,
    ),
)


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
        help="Limit orchestration to one configured blend name.",
    )
    parser.add_argument(
        "--ensemble_model",
        default=None,
        help="Optional exact filter for the ensemble model in a blend.",
    )
    parser.add_argument(
        "--deterministic_model",
        default=None,
        help="Optional exact filter for the deterministic model in a blend.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log eligible blend commands without executing them.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rerun blend and diagnostic outputs that already exist.",
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--blend-only",
        action="store_true",
        help="Run eligible blend outputs without diagnostics.",
    )
    mode.add_argument(
        "--diagnostics-only",
        action="store_true",
        help="Run eligible model diagnostics without blend outputs.",
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
    if blend.diagnostic_inputs is not None:
        diagnostic_roles = {input_.role for input_ in blend.diagnostic_inputs}
        if diagnostic_roles != {"deterministic", "ensemble"} or len(blend.diagnostic_inputs) != 2:
            raise ValueError(
                f"Blend {blend.name} must define exactly one deterministic and one ensemble diagnostic input."
            )
        if {input_.model for input_ in blend.diagnostic_inputs} != blend.models():
            raise ValueError(
                f"Blend {blend.name} diagnostic model names do not match its inputs."
            )


def select_blends(args: argparse.Namespace) -> list[BlendConfig]:
    model = args.model
    ensemble_model = args.ensemble_model
    deterministic_model = args.deterministic_model
    region = args.region.lower() if args.region else None
    blend_name = args.blend

    selected = []
    for blend in BLENDS:
        validate_blend_config(blend)
        if region and blend.region != region:
            continue
        if blend_name and blend.name != blend_name:
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


def missing_diagnostic_inputs(blend: BlendConfig, date: str) -> dict[str, Path]:
    return {
        model: path
        for model, path in blend.diagnostic_input_paths(date).items()
        if not path.exists()
    }


def run_blend(
    blend: BlendConfig,
    date: str,
    dry_run: bool,
    force: bool,
    debug: bool,
    skip_to: int | None,
) -> bool:
    if not blend.implemented:
        logger.info("Blend %s/%s is disabled; no blend coefficients are configured.", blend.region, blend.name)
        return False

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
    if output_dir.exists() and not force:
        logger.info(
            "Blend %s/%s already has output at %s; skipping. Use --force to rerun.",
            blend.region,
            blend.name,
            output_dir,
        )
        return False

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


def run_diagnostics(
    blend: BlendConfig,
    date: str,
    dry_run: bool,
    force: bool,
) -> bool:
    if not blend.diagnostic_plots:
        return False

    missing = missing_diagnostic_inputs(blend, date)
    if missing:
        logger.info(
            "Diagnostics %s/%s are not ready for %s. Missing: %s",
            blend.region,
            blend.name,
            date,
            ", ".join(f"{model}={path}" for model, path in missing.items()),
        )
        return False

    output_dir = blend.diagnostic_output_dir(date)
    if output_dir.exists() and not force:
        logger.info(
            "Diagnostics %s/%s already have output at %s; skipping. Use --force to rerun.",
            blend.region,
            blend.name,
            output_dir,
        )
        return False

    cmd = blend.diagnostics_command(date)
    logger.info("Diagnostics command: %s", " ".join(cmd))
    if dry_run:
        return True

    subprocess.run(cmd, check=True, cwd=REPO_ROOT / "model_diagnostics" / "utils")
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
        if not args.diagnostics_only:
            ran_any = (
                run_blend(
                    blend,
                    args.date,
                    dry_run=args.dry_run,
                    force=args.force,
                    debug=args.debug,
                    skip_to=args.skip_to,
                )
                or ran_any
            )
        if not args.blend_only:
            ran_any = (
                run_diagnostics(
                    blend,
                    args.date,
                    dry_run=args.dry_run,
                    force=args.force,
                )
                or ran_any
            )

    if not ran_any:
        logger.info("No blends were run for %s.", args.date)


if __name__ == "__main__":
    main()
