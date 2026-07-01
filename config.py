"""Configuration for the VWAP-cross intraday options backtester."""
import os
from datetime import time as _dtime
from dotenv import load_dotenv

load_dotenv()


def _parse_time(s, default):
    try:
        h, m = str(s).split(":")
        return _dtime(int(h), int(m))
    except Exception:
        return default


def _bool(v, default=False):
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # Dhan API (for fetching historical candles)
    DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "")
    DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")

    # Position sizing
    CAPITAL_PER_TRADE = float(os.getenv("CAPITAL_PER_TRADE", 15000))
    MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 4))

    # Live paper trading
    LOT_SIZE = int(os.getenv("LOT_SIZE", 75))          # NIFTY = 75
    ITM_DEPTH = int(os.getenv("ITM_DEPTH", 2))         # strikes ITM (2 ~= delta 0.6)
    UNDER_SCRIP = int(os.getenv("UNDER_SCRIP", 13))    # NIFTY index
    CANDLE_SECURITY_ID = os.getenv("CANDLE_SECURITY_ID", "13")

    # ---- REAL trading safety ----
    LIVE_TRADING = _bool(os.getenv("LIVE_TRADING"), False)   # master switch (keep False until proven)
    MAX_LOTS = int(os.getenv("MAX_LOTS", 1))                 # hard cap on lots per trade

    # ---- Position sizing (paper) ----
    # 0 = ALL-IN (old behaviour: buy as many lots as capital allows).
    # >0 = risk only this % of capital per trade (a -PREMIUM_STOP_PCT stop loses ~this %).
    RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", 0))

    # ---- VWAP-cross strategy (the validated one) ----
    VWAP_CROSS_POINTS = float(os.getenv("VWAP_CROSS_POINTS", 80))   # symmetric value used by sweeps
    VWAP_POINTS_EMA9 = float(os.getenv("VWAP_POINTS_EMA9", 60))     # 9 EMA distance from VWAP
    VWAP_POINTS_EMA15 = float(os.getenv("VWAP_POINTS_EMA15", 50))   # 15 EMA distance from VWAP
    # STATE = enter whenever condition true (more trades) | TRANSITION = only on the flip (selective)
    ENTRY_MODE = os.getenv("ENTRY_MODE", "TRANSITION")   # selective = real edge (OOS PF 1.89)
    PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", 0.5))  # index % move target
    HARD_STOP_PCT = float(os.getenv("HARD_STOP_PCT", 0.7))          # index % move stop
    BREAKEVEN_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TRIGGER_PCT", 0.25))
    # ---- PREMIUM-based exits (live paper/real use the REAL option premium) ----
    USE_PREMIUM_EXITS = _bool(os.getenv("USE_PREMIUM_EXITS"), True)
    PREMIUM_TARGET_PCT = float(os.getenv("PREMIUM_TARGET_PCT", 30))   # sell when premium +30%
    PREMIUM_STOP_PCT = float(os.getenv("PREMIUM_STOP_PCT", 20))       # exit when premium -20%
    PREMIUM_BE_PCT = float(os.getenv("PREMIUM_BE_PCT", 12))           # arm break-even after +12%
    PREMIUM_TRAIL_PCT = float(os.getenv("PREMIUM_TRAIL_PCT", 15))     # exit if premium falls this % from peak
    PREMIUM_TRAIL_ARM = float(os.getenv("PREMIUM_TRAIL_ARM", 20))     # trailing starts once premium is +this%
    USE_SR_EXIT = _bool(os.getenv("USE_SR_EXIT"), False)   # S&R exit cuts winners — OFF = let them run
    SR_PROXIMITY_PCT = float(os.getenv("SR_PROXIMITY_PCT", 0.15))  # % price must be within S&R
    SR_WINDOW = int(os.getenv("SR_WINDOW", 80))   # bars for RECENT swing S&R (not whole history)
    # Only trade in a trending market — skip sideways/choppy (ADX based)
    TRADE_ONLY_TRENDING = _bool(os.getenv("TRADE_ONLY_TRENDING"), True)
    ADX_MIN = float(os.getenv("ADX_MIN", 20))     # below this = sideways, no new trades
    # Circuit breaker: stop trading for the day after N losses in a row (bad day)
    MAX_CONSEC_LOSSES = int(os.getenv("MAX_CONSEC_LOSSES", 2))  # arm break-even after this

    # Exits / session
    INTRADAY_ONLY = _bool(os.getenv("INTRADAY_ONLY"), True)         # square off same day
    # No new entries before/after these IST times (skip volatile open + late session).
    # 9:15 open VWAP is unreliable -> wait until 9:45. Stop new trades by 15:00.
    NO_ENTRY_BEFORE = _parse_time(os.getenv("NO_ENTRY_BEFORE", "09:45"), _dtime(9, 45))
    NO_ENTRY_AFTER = _parse_time(os.getenv("NO_ENTRY_AFTER", "15:00"), _dtime(15, 0))
    THETA_DECAY_PER_BAR = float(os.getenv("THETA_DECAY_PER_BAR", 0.05))

    # Daily circuit breakers (0 = off)
    DAILY_PROFIT_TARGET = float(os.getenv("DAILY_PROFIT_TARGET", 0))
    DAILY_MAX_LOSS = float(os.getenv("DAILY_MAX_LOSS", 0))

    # Costs (Indian F&O charges modelled precisely in backtest; this is slippage)
    SLIPPAGE_PCT = float(os.getenv("SLIPPAGE_PCT", 0.1))            # % of capital each side


config = Config()
