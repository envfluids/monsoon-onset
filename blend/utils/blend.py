import pandas as pd
import numpy as np
from pathlib import Path



def blend(df_raw, date):
    base = Path(__file__).resolve().parent.parent
    coef_file = base / "data" / "coefs" / "multinom_coefs_full.csv"
    coef_clim_file = base / "data" / "coefs" / "multinom_coefs_full_clim.csv"
    output_dir = base / "output" / date
    # processed_data_file = "/scratch/midway3/marchakitus/monsoon-onset/blend/utils/intermediate/all_data.csv"
    # ------------------------------------------------------------------------------
    # 1) load raw data + both coefficient matrices
    # ------------------------------------------------------------------------------
    # df_raw = pd.read_csv(processed_data_file)

    # original model coefs
    coef_orig = pd.read_csv(coef_file) \
                .set_index("category")
    features_orig = coef_orig.columns.tolist()

    # clim model coefs (CSV this time)
    coef_clim = pd.read_csv(coef_clim_file) \
                .set_index("category")
    features_clim = coef_clim.columns.tolist()

    # ------------------------------------------------------------------------------
    # 2) figure out the full set of features we need
    # ------------------------------------------------------------------------------
    # include intercept explicitly
    union_feats = set(features_orig) | set(features_clim) | {"(Intercept)"}

    # ------------------------------------------------------------------------------
    # 3) construct design columns for every feature in the union
    # ------------------------------------------------------------------------------
    df = df_raw.copy()

    # intercept
    df["(Intercept)"] = 1

    # zero‐fill any missing main‐effect columns
    for feat in union_feats:
        if ":" not in feat and feat != "(Intercept)" and feat not in df:
            df[feat] = 0

    # build all needed interaction terms
    interaction_feats = [f for f in union_feats if ":" in f]
    for term in interaction_feats:
        parts = term.split(":")
        df[term] = 1
        for p in parts:
            if p not in df:
                raise ValueError(f"Missing main‐effect {p} for interaction {term}")
            df[term] *= df[p]

    # ------------------------------------------------------------------------------
    # 4) helper to compute softmax probabilities
    # ------------------------------------------------------------------------------
    def softmax_probs(X_mat, coefs_df):
        logits = X_mat.dot(coefs_df.values.T)
        exp_l  = np.exp(logits)
        return exp_l / exp_l.sum(axis=1, keepdims=True)

    # ------------------------------------------------------------------------------
    # 5) compute original‐model probabilities
    # ------------------------------------------------------------------------------
    X_orig = df[features_orig]
    probs_orig = softmax_probs(X_orig.values, coef_orig)
    prob_df_orig = pd.DataFrame(
        probs_orig,
        columns=coef_orig.index,
        index=df.index
    )

    # ------------------------------------------------------------------------------
    # 6) compute clim‐model probabilities
    # ------------------------------------------------------------------------------
    X_clim = df[features_clim]
    probs_clim = softmax_probs(X_clim.values, coef_clim)
    prob_df_clim = pd.DataFrame(
        probs_clim,
        columns=[f"clim_{c}" for c in coef_clim.index],
        index=df.index
    )

    # ------------------------------------------------------------------------------
    # 7) combine and write CSVs
    # ------------------------------------------------------------------------------
    # full raw + both sets of probs
    combined = pd.concat([
        df.reset_index(drop=True),
        prob_df_orig.reset_index(drop=True),
        prob_df_clim.reset_index(drop=True)
    ], axis=1)

    clim_out = output_dir / "blend_output_with_clim.csv"
    combined.to_csv(clim_out, index=False)

    # summary: only lat, lon, time + predictions
    keep = ["lat", "lon", "time"] \
        + list(coef_orig.index) \
        + [f"clim_{c}" for c in coef_clim.index]
    summary = combined[keep]

    summary_out = output_dir / "blend_output_summary.csv"
    summary.to_csv(summary_out, index=False)

    print("Wrote blend_output_with_clim.csv and blend_output_summary.csv to", output_dir)

    return summary
