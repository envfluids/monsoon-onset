"""Run settings for the AIFS ensemble (member count and forecast length).

Kept free of heavy imports so it can be tested without torch or anemoi.
Precedence: command-line argument, then environment variable, then default.
"""

import os

DEFAULT_N_MEMBERS = 25
DEFAULT_LEAD_TIME_HOURS = 24 * 50
STEP_HOURS = 6

N_MEMBERS_ENV = "AIFS_ENS_N_MEMBERS"
LEAD_TIME_HOURS_ENV = "AIFS_ENS_LEAD_TIME_HOURS"


def _resolve_int(arg_value, env_name, default, environ):
    if arg_value is not None:
        return int(arg_value), "argument"
    raw = environ.get(env_name, "").strip()
    if raw:
        try:
            return int(raw), env_name
        except ValueError:
            raise ValueError(f"{env_name} must be an integer, got {raw!r}") from None
    return default, "default"


def resolve_run_settings(n_members=None, lead_time_hours=None, environ=None):
    """Return (n_members, lead_time_hours) after validation."""
    environ = os.environ if environ is None else environ
    n_members, members_source = _resolve_int(
        n_members, N_MEMBERS_ENV, DEFAULT_N_MEMBERS, environ
    )
    lead_time_hours, lead_source = _resolve_int(
        lead_time_hours, LEAD_TIME_HOURS_ENV, DEFAULT_LEAD_TIME_HOURS, environ
    )

    if n_members < 1:
        raise ValueError(
            f"n_members must be at least 1 (from {members_source}), got {n_members}"
        )
    if lead_time_hours < STEP_HOURS or lead_time_hours % STEP_HOURS:
        raise ValueError(
            f"lead_time_hours must be a positive multiple of {STEP_HOURS} "
            f"(from {lead_source}), got {lead_time_hours}"
        )
    return n_members, lead_time_hours


def add_run_arguments(parser):
    parser.add_argument(
        "--n-members",
        type=int,
        default=None,
        help=f"Ensemble members to run (env {N_MEMBERS_ENV}, default {DEFAULT_N_MEMBERS})",
    )
    parser.add_argument(
        "--lead-time-hours",
        type=int,
        default=None,
        help=(
            f"Forecast length in hours, a multiple of {STEP_HOURS} "
            f"(env {LEAD_TIME_HOURS_ENV}, default {DEFAULT_LEAD_TIME_HOURS})"
        ),
    )
