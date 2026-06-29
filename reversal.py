"""
Support/Resistance + Reversal detection.

S&R: swing highs/lows over a lookback window.
Reversal signal: price within PROXIMITY of an S/R level AND
  a reversal candle (hammer/engulfing) AND the fast EMA (ema3) is turning.

Used by the live bot to EXIT early when a reversal is forming at S/R,
BEFORE the full hard stop is hit.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def adx_series(df: pd.DataFrame, period=14):
    """Full ADX series (vectorized) — used by the backtest per-bar without recompute."""
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/period, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/period, adjust=False).mean().fillna(0)


def adx_value(df: pd.DataFrame, period=14):
    """ADX trend-strength at the last bar. <20 = sideways, >25 = strong trend."""
    if len(df) < period * 2:
        return 0.0
    return float(adx_series(df, period).iloc[-1])


def market_regime(df: pd.DataFrame, adx_min=20):
    """Return ('TRENDING'|'SIDEWAYS', adx_value)."""
    a = adx_value(df)
    return ("TRENDING" if a >= adx_min else "SIDEWAYS"), round(a, 1)


def mean_reversion_signal(df: pd.DataFrame, supports, resistances, proximity_pct=0.15):
    """
    RANGE strategy (for sideways markets) — the OPPOSITE of trend trading:
      bounce at SUPPORT     -> BUY (CE), expect price to rise back up
      rejection at RESISTANCE -> SELL (PE), expect price to fall back down
    Returns ('BUY'|'SELL'|None, reason).
    """
    if len(df) < 3:
        return None, ""
    last = df.iloc[-1]
    close = last["close"]
    green = close > last["open"]
    red = close < last["open"]
    e3 = ema3(df["close"])
    e3_up = e3.iloc[-1] > e3.iloc[-2]
    near_s, s_lvl = near_level(close, supports, proximity_pct)
    near_r, r_lvl = near_level(close, resistances, proximity_pct)
    # bounce at support -> CE (only if support holding: close >= support)
    if near_s and close >= s_lvl and (green or e3_up):
        return "BUY", f"bounce at support {round(s_lvl, 1)}"
    # rejection at resistance -> PE (only if resistance holding: close <= resistance)
    if near_r and close <= r_lvl and (red or not e3_up):
        return "SELL", f"rejection at resistance {round(r_lvl, 1)}"
    return None, ""


def swing_levels(df: pd.DataFrame, left=5, right=3, n=3):
    """Return the last n swing highs and lows as S&R levels."""
    h, l = df["high"].values, df["low"].values
    highs, lows = [], []
    for i in range(left, len(df) - right):
        if h[i] == max(h[i-left:i+right+1]):
            highs.append(h[i])
        if l[i] == min(l[i-left:i+right+1]):
            lows.append(l[i])
    return sorted(highs)[-n:], sorted(lows)[:n]


def near_level(price, levels, proximity_pct=0.15):
    """True if price is within proximity% of any level."""
    for lvl in levels:
        if abs(price - lvl) / lvl * 100 <= proximity_pct:
            return True, lvl
    return False, None


def reversal_candle(row, side):
    """
    Detect a reversal candle for the given position side.
    side 'PE' (short) -> look for bullish reversal (hammer / bullish engulf).
    side 'CE' (long)  -> look for bearish reversal (shooting star / bear engulf).
    """
    o, c, h, l = row["open"], row["close"], row["high"], row["low"]
    body = abs(c - o)
    rng = h - l if h != l else 1e-9
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    if side == "PE":
        hammer = lower_wick > 2 * body and body / rng > 0.05
        bull_engulf = c > o and body / rng > 0.5
        return hammer or bull_engulf
    else:
        star = upper_wick > 2 * body and body / rng > 0.05
        bear_engulf = c < o and body / rng > 0.5
        return star or bear_engulf


def ema3(series, n=3):
    return series.ewm(span=n, adjust=False).mean()


def mature_sr_exit(df: pd.DataFrame, position_side: str,
                   supports: list, resistances: list,
                   proximity_pct: float = 0.15):
    """
    Mature S&R exit — only exit when bounce/rejection is CONFIRMED.
    PE at support: exit only if price CLOSES ABOVE support (bounce confirmed).
                   If price CLOSES BELOW support -> breakout, hold the PE.
    CE at resistance: exit only if price CLOSES BELOW resistance (rejection confirmed).
                      If price CLOSES ABOVE resistance -> breakout, hold the CE.
    """
    if len(df) < 3:
        return False, None, ""
    last, prev = df.iloc[-1], df.iloc[-2]
    close = last["close"]
    e3 = ema3(df["close"])
    e3_up = e3.iloc[-1] > e3.iloc[-2]
    green_candle = close > last["open"]
    red_candle = close < last["open"]
    if position_side == "PE":
        near, lvl = near_level(close, supports, proximity_pct)
        if not near:
            return False, None, ""
        if close < lvl:
            # breaking BELOW support = support broke, continue lower, HOLD PE
            return False, None, f"support {round(lvl,1)} breaking — hold PE"
        if close > lvl and (green_candle or e3_up):
            # closed ABOVE support with bullish confirmation = bounce confirmed, EXIT PE
            return True, lvl, f"bounce confirmed at support {round(lvl,1)}"
        return False, None, f"near support {round(lvl,1)} — waiting for confirmation"
    else:  # CE
        near, lvl = near_level(close, resistances, proximity_pct)
        if not near:
            return False, None, ""
        if close > lvl:
            # breaking ABOVE resistance = breakout, HOLD CE
            return False, None, f"resistance {round(lvl,1)} breaking — hold CE"
        if close < lvl and (red_candle or not e3_up):
            # closed BELOW resistance with bearish confirmation = rejection confirmed, EXIT CE
            return True, lvl, f"rejection confirmed at resistance {round(lvl,1)}"
        return False, None, f"near resistance {round(lvl,1)} — waiting for confirmation"


def detect_reversal(df: pd.DataFrame, position_side: str,
                    proximity_pct: float = 0.15):
    """
    Returns a dict:
      warning  : bool   - True if reversal is forming
      reason   : str    - why
      near_lvl : float  - the S&R level price is near (or None)
      supports : list   - detected support levels
      resistances: list - detected resistance levels
    """
    if len(df) < 20:
        return {"warning": False, "reason": "not enough data"}

    resistances, supports = swing_levels(df)
    last = df.iloc[-1]
    price = last["close"]

    # is price near support (for PE) or resistance (for CE)?
    if position_side == "PE":
        near, lvl = near_level(price, supports, proximity_pct)
    else:
        near, lvl = near_level(price, resistances, proximity_pct)

    # is ema3 turning against the trade?
    e3 = ema3(df["close"])
    ema_reversing = (e3.iloc[-1] > e3.iloc[-2]) if position_side == "PE" else (e3.iloc[-1] < e3.iloc[-2])

    # reversal candle on the current bar?
    rev_candle = reversal_candle(last, position_side)

    warning = near and (ema_reversing or rev_candle)
    reasons = []
    if near:
        reasons.append(f"price near {'support' if position_side=='PE' else 'resistance'} {round(lvl, 1)}")
    if ema_reversing:
        reasons.append("ema3 turning against trade")
    if rev_candle:
        reasons.append("reversal candle")

    return {
        "warning": warning,
        "reason": " + ".join(reasons) if reasons else "none",
        "near_lvl": round(lvl, 1) if lvl else None,
        "supports": [round(x, 1) for x in supports],
        "resistances": [round(x, 1) for x in resistances],
        "ema3_direction": "UP" if e3.iloc[-1] > e3.iloc[-2] else "DOWN",
        "reversal_candle": rev_candle,
    }
