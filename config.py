"""Configuration for the VWAP-cross intraday options backtester."""
import os
from dotenv import load_dotenv

load_dotenv()


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

    # ---- VWAP-cross strategy (the validated one) ----
    VWAP_CROSS_POINTS = float(os.getenv("VWAP_CROSS_POINTS", 80))   # both 9 & 15 EMA this far from VWAP
    PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", 0.5))  # index % move target
    HARD_STOP_PCT = float(os.getenv("HARD_STOP_PCT", 0.7))          # index % move stop
    BREAKEVEN_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TRIGGER_PCT", 0.25))  # arm break-even after this

    # Exits / session
    INTRADAY_ONLY = _bool(os.getenv("INTRADAY_ONLY"), True)         # square off same day
    THETA_DECAY_PER_BAR = float(os.getenv("THETA_DECAY_PER_BAR", 0.05))

    # Daily circuit breakers (0 = off)
    DAILY_PROFIT_TARGET = float(os.getenv("DAILY_PROFIT_TARGET", 0))
    DAILY_MAX_LOSS = float(os.getenv("DAILY_MAX_LOSS", 0))

    # Costs (Indian F&O charges modelled precisely in backtest; this is slippage)
    SLIPPAGE_PCT = float(os.getenv("SLIPPAGE_PCT", 0.1))            # % of capital each side


config = Config()
