"""
REAL money trading engine — places live orders via Dhan.

SAFETY:
  - Must be ARMED with the exact phrase "I UNDERSTAND THE RISK" to place orders.
  - MAX_LOTS hard cap per trade.
  - Kill switch: stop() squares off all open positions immediately.
  - Same strategy/exits as paper. Run paper FIRST for weeks before touching this.
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

ARM_PHRASE = "I UNDERSTAND THE RISK"
SYMBOL = "NIFTY"

STATE = {
    "running": False, "armed": False, "open": [], "closed": [], "log": [],
    "expiry": None, "spot": None, "last_bar": None, "realized": 0.0,
    "trades_today": 0, "trade_day": None, "params": {},
}
_stop = threading.Event()
_thread = None


def _log(msg):
    STATE["log"].insert(0, {"time": _now_ist().strftime("%H:%M:%S"), "msg": msg})
    STATE["log"] = STATE["log"][:200]


def _market_open():
    n = _now_ist()
    return n.weekday() < 5 and dtime(9, 15) <= n.time() <= dtime(15, 30)


def arm(phrase):
    STATE["armed"] = (phrase == ARM_PHRASE)
    _log("ARMED for live orders" if STATE["armed"] else "ARM failed (wrong phrase)")
    return STATE["armed"]


def start(lots, interval="3"):
    global _thread
    if STATE["running"]:
        return "already running"
    if not STATE["armed"]:
        return "NOT ARMED — type the exact arm phrase first"
    if lots < 1 or lots > config.MAX_LOTS:
        return f"lots must be 1..{config.MAX_LOTS}"
    try:
        exps = client.expiry_list(config.UNDER_SCRIP, "IDX_I")
        client._load_scrip()        # warm the scrip master
    except Exception as e:
        return f"startup failed: {e}"
    STATE.update(running=True, open=[], closed=[], log=[], last_bar=None, realized=0.0,
                 expiry=exps[0] if exps else None, params={"interval": str(interval), "lots": int(lots)})
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    _log(f"LIVE START — {lots} lot(s), expiry {STATE['expiry']}  ⚠ REAL MONEY")
    return "started"


def stop():
    """Kill switch: stop loop AND square off every open position now."""
    _stop.set()
    STATE["running"] = False
    try:
        chain = client.option_chain(STATE["expiry"], config.UNDER_SCRIP, "IDX_I")
        spot = client.parse_chain(chain)[0]
    except Exception:
        chain, spot = None, STATE["spot"]
    for pos in list(STATE["open"]):
        _exit(pos, spot, chain, "KILL_SWITCH")
    _log("LIVE STOP + squared off all")
    return "stopped"


def _loop():
    while not _stop.is_set():
        try:
            if _market_open():
                _cycle()
        except Exception as e:
            _log(f"loop error: {e}")
        _stop.wait(60)
    STATE["running"] = False


def _cycle():
    p = STATE["params"]
    df = client.historical_candles(config.CANDLE_SECURITY_ID, "IDX_I", "INDEX", p["interval"], days=2)
    if df is None or len(df) < 20:
        return
    sig = get_signals(df)
    last = sig.iloc[-1]
    bar_time = str(sig.index[-1])
    chain = None
    try:
        chain = client.option_chain(STATE["expiry"], config.UNDER_SCRIP, "IDX_I")
        STATE["spot"] = client.parse_chain(chain)[0]
    except Exception as e:
        _log(f"chain error: {e}")
    spot = STATE["spot"] or float(last["close"])
    _monitor(spot, chain)
    if bar_time != STATE["last_bar"]:
        STATE["last_bar"] = bar_time
        if last["signal"] in ("BUY", "SELL"):
            _on_signal(last["signal"], spot, chain)


def _on_signal(signal, spot, chain):
    want = "CE" if signal == "BUY" else "PE"
    for pos in list(STATE["open"]):
        if pos["opt_type"] != want:
            _exit(pos, spot, chain, "OPPOSITE")
    if any(p["opt_type"] == want for p in STATE["open"]) or chain is None:
        return
    today = _now_ist().date().isoformat()
    if STATE["trade_day"] != today:
        STATE["trade_day"] = today; STATE["trades_today"] = 0
    if STATE["trades_today"] >= config.MAX_TRADES_PER_DAY:
        return _log("daily trade limit reached")
    try:
        opt = client.pick_itm(chain, signal, depth=config.ITM_DEPTH)
        sid = client.option_security_id(SYMBOL, STATE["expiry"], opt["strike"], opt["type"])
    except Exception as e:
        return _log(f"strike/security_id error: {e}")
    qty = STATE["params"]["lots"] * config.LOT_SIZE
    if not (STATE["armed"] and config.LIVE_TRADING):
        return _log("blocked: not armed or LIVE_TRADING off")
    order = client.place_order(sid, qty, "BUY")
    ok = str(order.get("orderStatus", order.get("status", ""))).upper()
    STATE["open"].append({
        "signal": signal, "opt_type": opt["type"], "strike": opt["strike"],
        "security_id": sid, "lots": STATE["params"]["lots"], "qty": qty,
        "entry_ltp": opt["ltp"], "entry_spot": spot, "delta": round(opt["delta"], 3),
        "peak_fav": 0.0, "entry_time": _now_ist().strftime("%H:%M:%S"),
        "order": order,
    })
    STATE["trades_today"] += 1
    _log(f"LIVE BUY {opt['type']} {opt['strike']} x{STATE['params']['lots']}lot "
         f"@~{opt['ltp']} (order {ok})")


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
    if STATE["armed"] and config.LIVE_TRADING:
        order = client.place_order(pos["security_id"], pos["qty"], "SELL")
    else:
        order = {"status": "not-armed"}
    ltp = client.option_ltp(chain, pos["strike"], pos["opt_type"]) if chain else pos["entry_ltp"]
    if ltp <= 0:
        ltp = pos["entry_ltp"]
    pnl = (ltp - pos["entry_ltp"]) * pos["qty"]
    STATE["realized"] += pnl
    pos.update(exit_ltp=ltp, exit_reason=reason, pnl=round(pnl, 2),
               exit_time=_now_ist().strftime("%H:%M:%S"), exit_order=order)
    STATE["open"].remove(pos)
    STATE["closed"].insert(0, pos)
    _log(f"LIVE SELL [{reason}] {pos['opt_type']} {pos['strike']} @~{ltp} "
         f"est P&L Rs.{round(pnl)}")


def state_json():
    closed = STATE["closed"]
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    return {
        "running": STATE["running"], "armed": STATE["armed"],
        "live_trading": config.LIVE_TRADING, "spot": STATE["spot"], "expiry": STATE["expiry"],
        "realized": round(STATE["realized"], 2), "trades_today": STATE["trades_today"],
        "closed_count": len(closed), "win_rate": round(100 * wins / len(closed), 1) if closed else 0,
        "open": STATE["open"], "closed": closed[:50], "log": STATE["log"],
    }
