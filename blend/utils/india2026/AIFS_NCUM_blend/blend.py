"""
blend.py – Multinomial logistic blend of forecast features.

Coefficient file format (2026)
-------------------------------
The new coefficient file uses a tidy (long) format:

    term,estimate
    week1::(Intercept),-5.169842573
    week1::prob_clim_mr_week1,0.064936665
    week1::diff_aifs_week1,0.524149786
    ...

`term` encodes <interval>::<feature_name>.  This is pivoted internally to a
(n_intervals × n_features) matrix before prediction.

Missing features (e.g. diff_ngcm_* when NeuralGCM is not active) are
zero-filled automatically, so the coefficients for any missing model simply
have no effect on predictions.
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")


def _load_coef_tidy(path: Path) -> pd.DataFrame:
    """
    Load a tidy-format coefficient CSV and return a wide DataFrame.

    Input  columns : term  (e.g. "week1::diff_aifs_week1"), estimate
    Output          : DataFrame with interval as index, features as columns
    """
    df = pd.read_csv(path)
    df[["interval", "feature"]] = df["term"].str.split("::", n=1, expand=True)
    wide = df.pivot(index="interval", columns="feature", values="estimate")
    wide.columns.name = None
    wide.index.name   = None
    return wide


def _softmax_probs(X: np.ndarray, coefs: np.ndarray) -> np.ndarray:
    """
    Compute row-wise softmax probabilities.

    Parameters
    ----------
    X     : (n_rows, n_features)
    coefs : (n_classes, n_features)

    Returns
    -------
    (n_rows, n_classes) probabilities
    """
    logits = X @ coefs.T            # (n_rows, n_classes)
    exp_l  = np.exp(logits - logits.max(axis=1, keepdims=True))   # numerically stable
    return exp_l / exp_l.sum(axis=1, keepdims=True)


def blend(df_raw: pd.DataFrame, output_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Apply the blended logistic model to the feature table and write outputs.

    Parameters
    ----------
    df_raw     : Feature DataFrame produced by main.get_data()
    output_dir : Directory where blend_output_*.csv files are written.

    Returns
    -------
    (summary, summary_public)
      summary        – id, time, predicted-probability columns
      summary_public – summary rounded to 4 d.p., columns reordered
    """
    base = Path(__file__).resolve().parent.parent.parent.parent.parent  # monsoon-onset/

    coef_file = base / "blend" / "data" / "india2026" / "AIFS_NCUM_blend" / "data" / "coefs" / "coefs_full_model_subdistrict.csv"

    if not coef_file.exists():
        raise FileNotFoundError(f"Coefficient file not found: {coef_file}")

    # ── 1. Load and reshape coefficient matrix ────────────────────────────────
    coef_wide = _load_coef_tidy(coef_file)
    features  = coef_wide.columns.tolist()

    # Append the reference class ("later") as a row of zero coefficients so
    # softmax yields one probability per class where the week1..week4 row sums
    # are < 1 and the remainder falls into "later". The multinomial logit is
    # fit with "later" as the baseline, so its logit is identically 0.
    ref_class = "later"
    if ref_class not in coef_wide.index:
        coef_wide.loc[ref_class] = 0.0
    intervals  = coef_wide.index.tolist()

    # ── 2. Build design matrix ────────────────────────────────────────────────
    df = df_raw.copy()
    df["(Intercept)"] = 1.0

    # Zero-fill any feature column missing from the data
    # (e.g. diff_ngcm_* when NeuralGCM is not active)
    for feat in features:
        if ":" not in feat and feat not in df.columns:
            log.debug("Feature '%s' missing from data – zero-filling.", feat)
            df[feat] = 0.0

    # Build interaction terms (separated by ":")
    for feat in features:
        if ":" in feat:
            parts = feat.split(":")
            df[feat] = 1.0
            for p in parts:
                if p not in df.columns:
                    raise ValueError(
                        f"Main-effect column '{p}' required for interaction '{feat}' "
                        f"is missing from the data."
                    )
                df[feat] = df[feat] * df[p]

    # ── 3. Compute softmax probabilities ──────────────────────────────────────
    X        = df[features].values.astype(float)
    probs    = _softmax_probs(X, coef_wide.values.astype(float))
    prob_df  = pd.DataFrame(probs, columns=intervals, index=df.index)

    # ── 4. Combine and write outputs ──────────────────────────────────────────
    id_cols = ["id", "time"]

    # Full output: all input features + predicted probabilities
    combined = pd.concat(
        [df_raw.reset_index(drop=True), prob_df.reset_index(drop=True)],
        axis=1,
    )
    combined.to_csv(output_dir / "blend_output_full.csv", index=False)

    # Summary: id, time, predicted probabilities only
    summary_cols = id_cols + intervals
    summary      = combined[[c for c in summary_cols if c in combined.columns]].copy()
    summary.to_csv(output_dir / "blend_output_summary.csv", index=False)

    log.info("Blend outputs written to %s", output_dir)

    # Public version: round to 4 d.p., reorder columns
    summary_public = summary.copy().round(4)

    return summary, summary_public