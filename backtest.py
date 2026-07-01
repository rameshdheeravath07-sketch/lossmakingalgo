"""
Backtester for the VWAP-cross intraday options strategy.

Models the option as a delta instrument on the index, with realistic Indian F&O
charges (STT, GST, exchange, stamp, brokerage) + slippage. Exits: profit target,
break-even stop, hard stop, opposite signal, end-of-day square-off.
"""
from __future__ import annotations
from dataclasses import dataclass, field

import pandas as pd

from strategy import get_signals
from reversal import adx_series
from config import config
import engine


@dataclass
class Trade:
    entry_time: object
    side: str
    entry_price: float
    exit_time: object = None
    exit_price: float = None
    pnl_rupees: float = 0.0
    exit_reason: str = ""


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)

    def summary(self) -> dict:
        closed = [t for t in self.trades if t.exit_price is not None]
        wins = [t for t in closed if t.pnl_rupees > 0]
        total = sum(t.pnl_rupees for t in closed)
        gw = sum(t.pnl_rupees for t in wins)
        gl = -sum(t.pnl_rupees for t in closed if t.pnl_rupees <= 0)
        return {
            "trades": len(closed), "wins": len(wins), "losses": len(closed) - len(wins),
            "win_rate": round(100 * len(wins) / len(closed), 2) if closed else 0,
            "net_pnl": round(total, 2),
            "profit_factor": round(gw / gl, 2) if gl else None,
            "avg_pnl": round(total / len(closed), 2) if closed else 0,
        }


def run_backtest(df: pd.DataFrame, option_delta: float = 0.6, capital: float = None) -> BacktestResult:
    """Uses the SHARED engine — identical logic to paper + real trading."""
    capital = capital or config.CAPITAL_PER_TRADE
    decay = config.THETA_DECAY_PER_BAR
    sig = get_signals(df)
    adx_col = adx_series(df)                      # precomputed (fast)
    result = BacktestResult()
    equity = 0.0
    balance = capital          # COMPOUNDING balance (reinvests profits each trade)
    # position-sizing: fraction of balance deployed per trade (all-in = 1.0)
    deploy = 1.0 if config.RISK_PER_TRADE_PCT <= 0 else \
        min(1.0, config.RISK_PER_TRADE_PCT / config.PREMIUM_STOP_PCT)

    open_side = None
    entry_spot = entry_prem = 0.0
    entry_ts = None
    bars_held = 0
    peak_pct = 0.0

    def premium(spot):
        move = (spot - entry_spot) * (1 if open_side == "LONG" else -1)
        return max(entry_prem + move * option_delta - decay * bars_held, 0.05)

    def close(ts, prem, reason):
        nonlocal equity, day_pnl, consec_losses, balance
        ret = max((prem - entry_prem) / entry_prem, -1.0)
        position = max(balance * deploy, 0.0)          # reinvest: size off CURRENT balance
        pnl = ret * position
        buy_val, sell_val = position, position * (1 + ret)
        charges = 40 + 0.001 * sell_val + 0.0003503 * (buy_val + sell_val) + 0.00003 * buy_val \
            + 0.18 * (40 + 0.0003503 * (buy_val + sell_val)) + 2 * (config.SLIPPAGE_PCT / 100) * position
        pnl -= charges
        equity += pnl
        balance += pnl                                 # COMPOUND
        day_pnl += pnl
        consec_losses = consec_losses + 1 if pnl < 0 else 0
        result.trades.append(Trade(entry_ts, open_side, round(entry_spot, 2),
                                   ts, round(spot_at_exit, 2), round(pnl, 2), reason))

    prev_day = None
    trades_today = consec_losses = 0
    day_pnl = 0.0
    spot_at_exit = 0.0
    n = len(sig)
    for i in range(n):
        ts = sig.index[i]
        row = sig.iloc[i]
        c, hi, lo = row["close"], row["high"], row["low"]
        cur_day = ts.date() if hasattr(ts, "date") else None
        dslice = df.iloc[max(0, i - config.SR_WINDOW):i + 1]   # recent window for S&R

        if cur_day != prev_day:
            trades_today = 0; day_pnl = 0.0; consec_losses = 0
            if config.INTRADAY_ONLY and open_side is not None and prev_day is not None:
                spot_at_exit = prev_close
                close(prev_ts, premium(prev_close), "EOD_SQUAREOFF")
                open_side = None
        prev_day, prev_close, prev_ts = cur_day, c, ts

        # ---- EXITS (shared engine; intrabar stop overlay for risk realism) ----
        if open_side is not None:
            bars_held += 1
            opt_type = "CE" if open_side == "LONG" else "PE"
            fav_spot = hi if open_side == "LONG" else lo
            adv_spot = lo if open_side == "LONG" else hi
            prem_fav, prem_adv, prem_close = premium(fav_spot), premium(adv_spot), premium(c)
            peak_pct = max(peak_pct, (prem_fav - entry_prem) / entry_prem * 100)
            exit_prem = exit_reason = None
            stop_prem = entry_prem * (1 - config.PREMIUM_STOP_PCT / 100)
            if config.USE_PREMIUM_EXITS and prem_adv <= stop_prem:
                exit_prem, exit_reason, spot_at_exit = stop_prem, f"STOP (premium -{config.PREMIUM_STOP_PCT}%)", adv_spot
            else:
                ex, reason = engine.decide_exit(dslice, opt_type, entry_prem, prem_close, peak_pct, eod=False)
                if ex:
                    # target uses the bar's best premium; everything else uses close
                    if reason.startswith("TARGET"):
                        exit_prem, spot_at_exit = max(prem_close, prem_fav), fav_spot
                    else:
                        exit_prem, spot_at_exit = prem_close, c
                    exit_reason = reason
            if exit_prem is not None:
                close(ts, exit_prem, exit_reason)
                open_side = None

        # ---- ENTRY (shared engine: signal + regime + mean-reversion + gates) ----
        adx = float(adx_col.iloc[i])
        side, mode, regime = engine.decide_entry(row, dslice, adx)
        if side:
            new_side = "LONG" if side == "BUY" else "SHORT"
            if open_side is not None and open_side != new_side:
                spot_at_exit = c
                close(ts, premium(c), "OPPOSITE")
                open_side = None
            if open_side is None:
                ist_t = (ts + pd.Timedelta(hours=5, minutes=30)).time()
                ok_gate, _ = engine.entry_gate(dslice, side, mode, ist_t)
                ok_risk, _ = engine.can_trade(trades_today, consec_losses, day_pnl)
                if ok_gate and ok_risk:
                    open_side = new_side
                    entry_spot = c
                    entry_prem = max(c * 2.0 / 100, 1)
                    peak_pct = 0.0
                    entry_ts = ts
                    bars_held = 0
                    trades_today += 1

        result.equity_curve.append({"time": str(ts), "equity": round(equity, 2)})

    if open_side is not None:
        spot_at_exit = sig.iloc[-1]["close"]
        close(sig.index[-1], premium(spot_at_exit), "EOD")
    return result


def _set_vwap(v):
    config.VWAP_POINTS_EMA9 = v
    config.VWAP_POINTS_EMA15 = v


def sweep_vwap_threshold(df, thresholds=(20, 30, 40, 50, 60, 80, 100, 120, 150),
                         capital=None, option_delta=0.6):
    rows = []
    orig = (config.VWAP_POINTS_EMA9, config.VWAP_POINTS_EMA15)
    try:
        for thr in thresholds:
            _set_vwap(thr)
            rows.append({"threshold": thr, **run_backtest(df, option_delta, capital).summary()})
    finally:
        config.VWAP_POINTS_EMA9, config.VWAP_POINTS_EMA15 = orig
    rows.sort(key=lambda r: r["net_pnl"], reverse=True)
    return rows


def sweep_target(df, targets=(0.2, 0.3, 0.5, 0.75, 1.0, 1.5), capital=None, option_delta=0.6):
    rows = []
    orig = config.PROFIT_TARGET_PCT
    try:
        for tp in targets:
            config.PROFIT_TARGET_PCT = tp
            rows.append({"target_pct": tp, **run_backtest(df, option_delta, capital).summary()})
    finally:
        config.PROFIT_TARGET_PCT = orig
    rows.sort(key=lambda r: r["net_pnl"], reverse=True)
    return rows


def walk_forward(df, train_frac=0.6, thresholds=(40, 50, 60, 80, 100), capital=None, option_delta=0.6):
    cut = int(len(df) * train_frac)
    train, test = df.iloc[:cut], df.iloc[cut:]
    best = sweep_vwap_threshold(train, thresholds, capital, option_delta)[0]
    orig = (config.VWAP_POINTS_EMA9, config.VWAP_POINTS_EMA15)
    try:
        _set_vwap(best["threshold"])
        test_res = run_backtest(test, option_delta, capital).summary()
    finally:
        config.VWAP_POINTS_EMA9, config.VWAP_POINTS_EMA15 = orig
    return {
        "train_best_threshold": best["threshold"],
        "train_summary": {k: best[k] for k in ("trades", "win_rate", "profit_factor", "net_pnl")},
        "test_summary": test_res,
        "train_period": [str(train.index[0]), str(train.index[-1])],
        "test_period": [str(test.index[0]), str(test.index[-1])],
        "verdict": "ROBUST" if test_res["net_pnl"] > 0 else "CURVE-FIT (test lost)",
    }
