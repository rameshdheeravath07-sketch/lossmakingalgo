"""
Single shared DECISION ENGINE.

Backtest, paper, and real ALL call these functions, so the strategy logic is
identical everywhere — what you backtest is exactly what you paper-trade is
exactly what you trade live.

Premium source differs by caller (backtest = delta proxy, live = real LTP),
so premium values are passed IN; the decision logic is shared.
"""
from __future__ import annotations
from config import config
from reversal import (swing_levels, mean_reversion_signal, mature_sr_exit)


def recent_sr(df):
    """RECENT swing S&R (last SR_WINDOW bars) — fixes the 90-day-extremes bug."""
    w = config.SR_WINDOW
    sub = df.tail(w) if len(df) > w else df
    return swing_levels(sub)          # (resistances, supports)


def regime_from_adx(adx):
    return "TRENDING" if adx >= config.ADX_MIN else "SIDEWAYS"


def size_lots(capital, entry_ltp):
    """How many lots to buy. RISK_PER_TRADE_PCT=0 -> all-in (as many as affordable).
    Otherwise size so a -PREMIUM_STOP_PCT stop loses ~RISK_PER_TRADE_PCT% of capital.
    Never exceeds what's affordable; floors at 1 lot if at least one is affordable."""
    cost_lot = entry_ltp * config.LOT_SIZE
    if cost_lot <= 0:
        return 0
    affordable = int(capital // cost_lot)
    if affordable < 1:
        return 0
    if config.RISK_PER_TRADE_PCT <= 0:
        return affordable                         # all-in (old behaviour)
    risk_budget = capital * config.RISK_PER_TRADE_PCT / 100.0
    per_lot_risk = cost_lot * config.PREMIUM_STOP_PCT / 100.0
    lots = int(risk_budget // per_lot_risk) if per_lot_risk > 0 else affordable
    lots = min(lots, affordable)
    return max(lots, 1)                            # small capital -> at least 1 lot


def decide_entry(last_row, df, adx):
    """
    Decide direction + mode from current conditions.
    last_row: the latest signal row (has both_above/both_below).
    df: candles (for recent S&R / mean reversion).  adx: precomputed ADX.
    Returns (side 'BUY'|'SELL'|None, mode 'TREND'|'RANGE', regime).
    """
    regime = regime_from_adx(adx)
    if regime == "TRENDING":
        if config.ENTRY_MODE == "TRANSITION":
            side = "BUY" if last_row.get("above_new") else "SELL" if last_row.get("below_new") else None
        else:
            side = "BUY" if last_row.get("both_above") else "SELL" if last_row.get("both_below") else None
        return side, "TREND", regime
    # sideways: either SKIP (trade only trends) or MEAN-REVERT
    if config.TRADE_ONLY_TRENDING:
        return None, "RANGE", regime
    res, sup = recent_sr(df)
    side, _msg = mean_reversion_signal(df, sup, res, config.SR_PROXIMITY_PCT)
    return side, "RANGE", regime


def entry_gate(df, side, mode, ist_time):
    """Time-window + (trend-mode) S&R entry gate. Returns (ok, reason)."""
    if not (config.NO_ENTRY_BEFORE <= ist_time <= config.NO_ENTRY_AFTER):
        return False, "outside trade window"
    if mode == "TREND":
        res, sup = recent_sr(df)
        want = "CE" if side == "BUY" else "PE"
        block, _lvl, msg = mature_sr_exit(df, want, sup, res, config.SR_PROXIMITY_PCT)
        if block:
            return False, f"skip {want}: {msg}"
    return True, ""


def can_trade(trades_today, consec_losses, day_pnl):
    """Risk-counter gates (daily limit, circuit breaker, daily P&L). (ok, reason)."""
    if trades_today >= config.MAX_TRADES_PER_DAY:
        return False, "daily trade limit"
    if config.MAX_CONSEC_LOSSES > 0 and consec_losses >= config.MAX_CONSEC_LOSSES:
        return False, f"halted: {consec_losses} losses in a row"
    if config.DAILY_PROFIT_TARGET > 0 and day_pnl >= config.DAILY_PROFIT_TARGET:
        return False, "daily profit target hit"
    if config.DAILY_MAX_LOSS > 0 and day_pnl <= -config.DAILY_MAX_LOSS:
        return False, "daily max loss hit"
    return True, ""


def decide_exit(df, opt_type, entry_prem, cur_prem, peak_prem_pct, eod):
    """
    Shared exit decision. cur_prem is the CURRENT premium (delta proxy in
    backtest, real LTP live). Returns (should_exit, reason).
    """
    prem_pct = (cur_prem - entry_prem) / entry_prem * 100
    if eod:
        return True, "EOD_SQUAREOFF"
    if config.USE_PREMIUM_EXITS:
        # target only if set (0 = no target, let winner run to stop/trail/EOD)
        if config.PREMIUM_TARGET_PCT > 0 and prem_pct >= config.PREMIUM_TARGET_PCT:
            return True, f"TARGET (premium +{round(prem_pct)}%)"
        if prem_pct <= -config.PREMIUM_STOP_PCT:
            return True, f"STOP (premium {round(prem_pct)}%)"
        if config.PREMIUM_BE_PCT > 0 and peak_prem_pct >= config.PREMIUM_BE_PCT and prem_pct <= 0:
            return True, "BREAKEVEN"
        # TRAILING stop on premium — captures the move, exits when it falls from peak.
        # Works independently (rides the winner up, books it before it gives back).
        if config.PREMIUM_TRAIL_PCT > 0 and peak_prem_pct >= config.PREMIUM_TRAIL_ARM \
                and (peak_prem_pct - prem_pct) >= config.PREMIUM_TRAIL_PCT:
            return True, f"TRAIL (locked, peak was +{round(peak_prem_pct)}%)"
    if config.USE_SR_EXIT and df is not None and len(df) >= 10:
        res, sup = recent_sr(df)
        se, _lvl, msg = mature_sr_exit(df, opt_type, sup, res, config.SR_PROXIMITY_PCT)
        if se:
            return True, f"SR_EXIT ({msg})"
    return False, ""
