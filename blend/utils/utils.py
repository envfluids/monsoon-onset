import numpy as np
import pandas as pd

def find_onset(series: np.ndarray, window: int, thresh: float) -> float:
    """
    Return 1-based index of first day where series>1 and rolling sum>thresh, else NaN.
    """
    if series.size < window:
        return np.nan
    roll_sum = pd.Series(series).rolling(window, min_periods=window).sum().to_numpy()
    cond = (series > 1) & (roll_sum > thresh)
    idx = np.where(cond)[0]
    return float(idx[0] + 1) if idx.size > 0 else np.nan


def compute_quasi_onset(series: np.ndarray, window: int, thresh: float) -> np.ndarray:
    """
    Boolean array: True where series>1 and rolling sum>thresh.
    """
    n = series.size
    if n < window:
        return np.zeros(n, dtype=bool)
    roll_sum = pd.Series(series).rolling(window, min_periods=window).sum().to_numpy()
    valid = (series > 1) & (roll_sum > thresh)
    return np.nan_to_num(valid, False).astype(bool)


def compute_roll_sum(arr: np.ndarray, n: int) -> np.ndarray:
    """
    Left-aligned rolling sum; positions without full window return NaN.
    """
    s = pd.Series(arr)
    rolled = s.rolling(n, min_periods=n).sum()
    #align left instead of right
    rolled = rolled.shift(-(n-1))
    return rolled.to_numpy()


