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
from config import config


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
    capital = capital or config.CAPITAL_PER_TRADE
    decay = config.THETA_DECAY_PER_BAR
    sig = get_signals(df)
    result = BacktestResult()
    equity = 0.0

    open_side = None
    entry_spot = entry_prem = 0.0
    entry_ts = None
    bars_held = 0
    be_armed = False

    def premium(spot):
        move = (spot - entry_spot) * (1 if open_side == "LONG" else -1)
        return max(entry_prem + move * option_delta - decay * bars_held, 0.05)

    def close(ts, prem, reason):
        nonlocal equity, day_pnl
        ret = max((prem - entry_prem) / entry_prem, -1.0)
        pnl = ret * capital
        # realistic Indian F&O options charges on premium turnover
        buy_val, sell_val = capital, capital * (1 + ret)
        brokerage = 40
        stt = 0.001 * sell_val
        txn = 0.0003503 * (buy_val + sell_val)
        stamp = 0.00003 * buy_val
        gst = 0.18 * (brokerage + txn)
        slippage = 2 * (config.SLIPPAGE_PCT / 100) * capital
        pnl -= (brokerage + stt + txn + stamp + gst + slippage)
        equity += pnl
        day_pnl += pnl
        result.trades.append(Trade(entry_ts, open_side, round(entry_spot, 2),
                                   ts, round(spot_at_exit, 2), round(pnl, 2), reason))

    prev_day = None
    trades_today = 0
    day_pnl = 0.0
    spot_at_exit = 0.0
    for ts, row in sig.iterrows():
        s = row["signal"]
        c, hi, lo = row["close"], row["high"], row["low"]
        cur_day = ts.date() if hasattr(ts, "date") else None

        if cur_day != prev_day:
            trades_today = 0
            day_pnl = 0.0
            if config.INTRADAY_ONLY and open_side is not None and prev_day is not None:
                spot_at_exit = prev_close
                close(prev_ts, premium(prev_close), "EOD_SQUAREOFF")
                open_side = None
        prev_day, prev_close, prev_ts = cur_day, c, ts

        if open_side is not None:
            bars_held += 1
            fav_spot = hi if open_side == "LONG" else lo
            adv_spot = lo if open_side == "LONG" else hi
            exit_prem = exit_reason = None

            # profit target
            fav_move = (fav_spot - entry_spot) if open_side == "LONG" else (entry_spot - fav_spot)
            if config.PROFIT_TARGET_PCT > 0 and fav_move / entry_spot * 100 >= config.PROFIT_TARGET_PCT:
                tgt = entry_spot * (1 + config.PROFIT_TARGET_PCT / 100) if open_side == "LONG" \
                    else entry_spot * (1 - config.PROFIT_TARGET_PCT / 100)
                exit_prem, exit_reason, spot_at_exit = premium(tgt), "TARGET", tgt

            # break-even stop
            if exit_prem is None and config.BREAKEVEN_TRIGGER_PCT > 0:
                if fav_move / entry_spot * 100 >= config.BREAKEVEN_TRIGGER_PCT:
                    be_armed = True
                back = (adv_spot <= entry_spot) if open_side == "LONG" else (adv_spot >= entry_spot)
                if be_armed and back:
                    exit_prem, exit_reason, spot_at_exit = premium(entry_spot), "BREAKEVEN", entry_spot

            # hard stop
            if exit_prem is None and config.HARD_STOP_PCT > 0:
                adv_move = (entry_spot - adv_spot) if open_side == "LONG" else (adv_spot - entry_spot)
                if adv_move / entry_spot * 100 >= config.HARD_STOP_PCT:
                    exit_prem, exit_reason, spot_at_exit = premium(adv_spot), "HARD_STOP", adv_spot

            if exit_prem is not None:
                close(ts, exit_prem, exit_reason)
                open_side = None

        # entry / opposite-signal reverse
        if s in ("BUY", "SELL"):
            new_side = "LONG" if s == "BUY" else "SHORT"
            if open_side is not None and open_side != new_side:
                spot_at_exit = c
                close(ts, premium(c), "OPPOSITE")
                open_side = None
            tgt_hit = config.DAILY_PROFIT_TARGET > 0 and day_pnl >= config.DAILY_PROFIT_TARGET
            loss_hit = config.DAILY_MAX_LOSS > 0 and day_pnl <= -config.DAILY_MAX_LOSS
            if open_side is None and trades_today < config.MAX_TRADES_PER_DAY and not tgt_hit and not loss_hit:
                open_side = new_side
                entry_spot = c
                entry_prem = max(c * 2.0 / 100, 1)   # assume entry premium ~2% of spot
                entry_ts = ts
                bars_held = 0
                be_armed = False
                trades_today += 1

        result.equity_curve.append({"time": str(ts), "equity": round(equity, 2)})

    if open_side is not None:
        spot_at_exit = sig.iloc[-1]["close"]
        close(sig.index[-1], premium(spot_at_exit), "EOD")
    return result


def sweep_vwap_threshold(df, thresholds=(20, 30, 40, 50, 60, 80, 100, 120, 150),
                         capital=None, option_delta=0.6):
    rows = []
    orig = config.VWAP_CROSS_POINTS
    try:
        for thr in thresholds:
            config.VWAP_CROSS_POINTS = thr
            rows.append({"threshold": thr, **run_backtest(df, option_delta, capital).summary()})
    finally:
        config.VWAP_CROSS_POINTS = orig
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
    orig = config.VWAP_CROSS_POINTS
    try:
        config.VWAP_CROSS_POINTS = best["threshold"]
        test_res = run_backtest(test, option_delta, capital).summary()
    finally:
        config.VWAP_CROSS_POINTS = orig
    return {
        "train_best_threshold": best["threshold"],
        "train_summary": {k: best[k] for k in ("trades", "win_rate", "profit_factor", "net_pnl")},
        "test_summary": test_res,
        "train_period": [str(train.index[0]), str(train.index[-1])],
        "test_period": [str(test.index[0]), str(test.index[-1])],
        "verdict": "ROBUST" if test_res["net_pnl"] > 0 else "CURVE-FIT (test lost)",
    }
