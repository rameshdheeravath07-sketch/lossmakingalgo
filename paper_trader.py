"""
Live PAPER-trading engine for the VWAP-cross strategy.

No real orders are placed. It pulls live 3-min candles + the live option chain,
picks the real ITM strike (real delta, real premium), "buys" whole lots sized to
your CURRENT capital (compounding), and tracks real P&L using live option LTP.

Exits mirror the backtest: profit target, break-even, hard stop, opposite signal,
3:15 PM square-off. Runs in a background thread during market hours (IST).
"""
from __future__ import annotations
import threading
from datetime import datetime, time as dtime
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = None

from config import config
from dhan_client import client
from strategy import get_signals


def _now_ist():
    return datetime.now(IST) if IST else datetime.now()

STATE = {
    "running": False, "capital": 0.0, "start_capital": 0.0, "spot": None,
    "expiry": None, "open": [], "closed": [], "log": [],
    "last_bar": None, "trades_today": 0, "trade_day": None, "params": {},
}
_stop = threading.Event()
_thread = None


def _log(msg):
    STATE["log"].insert(0, {"time": _now_ist().strftime("%H:%M:%S"), "msg": msg})
    STATE["log"] = STATE["log"][:200]


def _market_open():
    n = _now_ist()
    if n.weekday() >= 5:
        return False
    return dtime(9, 15) <= n.time() <= dtime(15, 30)


def _check_day():
    today = _now_ist().date().isoformat()
    if STATE["trade_day"] != today:
        STATE["trade_day"] = today
        STATE["trades_today"] = 0


def start(capital, interval="3", ignore_hours=False):
    global _thread
    if STATE["running"]:
        return "already running"
    try:
        exps = client.expiry_list(config.UNDER_SCRIP, "IDX_I")
    except Exception as e:
        return f"expiry fetch failed: {e}"
    STATE.update(running=True, capital=float(capital), start_capital=float(capital),
                 open=[], closed=[], log=[], last_bar=None,
                 expiry=exps[0] if exps else None,
                 params={"interval": str(interval), "ignore_hours": ignore_hours})
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    _log(f"PAPER START — capital Rs.{capital}, expiry {STATE['expiry']}")
    return "started"


def stop():
    _stop.set()
    STATE["running"] = False
    _log("PAPER STOP")
    return "stopped"


def _loop():
    while not _stop.is_set():
        try:
            if STATE["params"].get("ignore_hours") or _market_open():
                _cycle()
        except Exception as e:
            _log(f"loop error: {e}")
        _stop.wait(60)          # 60s cycle (respects option-chain 1/3s limit)
    STATE["running"] = False


def _cycle():
    p = STATE["params"]
    df = client.historical_candles(config.CANDLE_SECURITY_ID, "IDX_I", "INDEX",
                                   p["interval"], days=2)
    if df is None or len(df) < 20:
        return
    sig = get_signals(df)
    last = sig.iloc[-1]
    spot = float(last["close"])
    STATE["spot"] = spot
    bar_time = str(sig.index[-1])

    chain = None
    try:
        chain = client.option_chain(STATE["expiry"], config.UNDER_SCRIP, "IDX_I")
        STATE["spot"] = client.parse_chain(chain)[0] or spot
    except Exception as e:
        _log(f"chain error: {e}")

    _monitor(STATE["spot"], chain)

    if bar_time != STATE["last_bar"]:
        STATE["last_bar"] = bar_time
        if last["signal"] in ("BUY", "SELL"):
            _on_signal(last["signal"], STATE["spot"], chain)


def _on_signal(signal, spot, chain):
    want = "CE" if signal == "BUY" else "PE"
    for pos in list(STATE["open"]):
        if pos["opt_type"] != want:
            _exit(pos, spot, chain, "OPPOSITE")
    if any(p["opt_type"] == want for p in STATE["open"]):
        return
    _check_day()
    if STATE["trades_today"] >= config.MAX_TRADES_PER_DAY:
        return _log("daily trade limit reached")
    if chain is None:
        return
    try:
        opt = client.pick_itm(chain, signal, depth=config.ITM_DEPTH)
    except Exception as e:
        return _log(f"strike pick error: {e}")
    ltp = opt["ltp"]
    if ltp <= 0:
        return _log(f"strike {opt['strike']} has no LTP; skip")
    cost_lot = ltp * config.LOT_SIZE
    lots = int(STATE["capital"] // cost_lot)
    if lots < 1:
        return _log(f"capital Rs.{round(STATE['capital'])} < 1 lot (Rs.{round(cost_lot)}); skip")
    qty = lots * config.LOT_SIZE
    STATE["open"].append({
        "signal": signal, "opt_type": opt["type"], "strike": opt["strike"],
        "lots": lots, "qty": qty, "entry_ltp": ltp, "entry_spot": spot,
        "delta": round(opt["delta"], 3), "peak_fav": 0.0,
        "entry_time": _now_ist().strftime("%H:%M:%S"),
    })
    STATE["trades_today"] += 1
    _log(f"ENTRY {signal} {opt['type']} {opt['strike']} x{lots}lot @Rs.{ltp} "
         f"(cost Rs.{round(cost_lot*lots)}, delta {round(opt['delta'],2)})")


def _monitor(spot, chain):
    if spot is None:
        return
    eod = _now_ist().time() >= dtime(15, 15)
    for pos in list(STATE["open"]):
        fav = (spot - pos["entry_spot"]) if pos["opt_type"] == "CE" else (pos["entry_spot"] - spot)
        fav_pct = fav / pos["entry_spot"] * 100
        pos["peak_fav"] = max(pos["peak_fav"], fav_pct)
        reason = None
        if eod:
            reason = "EOD_SQUAREOFF"
        elif fav_pct >= config.PROFIT_TARGET_PCT:
            reason = "TARGET"
        elif -fav_pct >= config.HARD_STOP_PCT:
            reason = "HARD_STOP"
        elif config.BREAKEVEN_TRIGGER_PCT > 0 and pos["peak_fav"] >= config.BREAKEVEN_TRIGGER_PCT and fav_pct <= 0:
            reason = "BREAKEVEN"
        if reason:
            _exit(pos, spot, chain, reason)


def _exit(pos, spot, chain, reason):
    ltp = client.option_ltp(chain, pos["strike"], pos["opt_type"]) if chain else 0
    if ltp <= 0:
        ltp = pos["entry_ltp"]
    pnl = (ltp - pos["entry_ltp"]) * pos["qty"]
    bv, sv = pos["entry_ltp"] * pos["qty"], ltp * pos["qty"]
    charges = 40 + 0.001 * sv + 0.0003503 * (bv + sv) + 0.00003 * bv + 0.18 * (40 + 0.0003503 * (bv + sv))
    pnl -= charges
    STATE["capital"] += pnl
    pos.update(exit_ltp=ltp, exit_reason=reason, pnl=round(pnl, 2),
               exit_time=_now_ist().strftime("%H:%M:%S"))
    STATE["open"].remove(pos)
    STATE["closed"].insert(0, pos)
    _log(f"EXIT [{reason}] {pos['opt_type']} {pos['strike']} @Rs.{ltp} "
         f"P&L Rs.{round(pnl)} | capital Rs.{round(STATE['capital'])}")


def state_json():
    closed = STATE["closed"]
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    return {
        "running": STATE["running"], "capital": round(STATE["capital"], 2),
        "start_capital": STATE["start_capital"], "spot": STATE["spot"],
        "expiry": STATE["expiry"], "trades_today": STATE["trades_today"],
        "pnl": round(STATE["capital"] - STATE["start_capital"], 2),
        "closed_count": len(closed), "wins": wins,
        "win_rate": round(100 * wins / len(closed), 1) if closed else 0,
        "open": STATE["open"], "closed": closed[:50], "log": STATE["log"],
    }
