"""
VWAP-cross strategy — the validated one.

Entry: both 9 EMA and 15 EMA at least VWAP_CROSS_POINTS away from VWAP
  - both ABOVE VWAP  -> BUY  (CE)
  - both BELOW VWAP  -> SELL (PE)
Fires on the bar the condition first becomes true.
Exits (handled in backtest): profit target, break-even, hard stop, opposite
signal, end-of-day square-off.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from config import config


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP, reset each calendar day (falls back to cumulative)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, np.nan).fillna(1)
    try:
        days = pd.Series(df.index.date, index=df.index)
        pv = (tp * vol).groupby(days).cumsum()
        vv = vol.groupby(days).cumsum()
    except Exception:
        pv = (tp * vol).cumsum()
        vv = vol.cumsum()
    return pv / vv


def get_signals(df: pd.DataFrame, **_) -> pd.DataFrame:
    out = df.copy()
    out["ema9"] = ema(out["close"], 9)
    out["ema15"] = ema(out["close"], 15)
    out["vwap"] = session_vwap(out)
    thr = config.VWAP_CROSS_POINTS

    both_above = (out["ema9"] >= out["vwap"] + thr) & (out["ema15"] >= out["vwap"] + thr)
    both_below = (out["ema9"] <= out["vwap"] - thr) & (out["ema15"] <= out["vwap"] - thr)

    buy = both_above & ~both_above.shift(1, fill_value=False)
    sell = both_below & ~both_below.shift(1, fill_value=False)

    signal = pd.Series([None] * len(out), index=out.index, dtype=object)
    signal[buy] = "BUY"
    signal[sell] = "SELL"
    out["signal"] = signal
    return out
