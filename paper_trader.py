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
from reversal import adx_value
import engine
import persistence


def _now_ist():
    return datetime.now(IST) if IST else datetime.now()

STATE = {
    "running": False, "capital": 0.0, "start_capital": 0.0, "spot": None,
    "expiry": None, "open": [], "closed": [], "log": [],
    "last_bar": None, "trades_today": 0, "trade_day": None, "params": {},
    "consec_losses": 0, "halted": False, "day_pnl": 0.0, "regime": None, "adx": 0,
    "day_start_capital": 0.0, "day_wins": 0, "auto": False,
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
        STATE["consec_losses"] = 0
        STATE["halted"] = False
        STATE["day_pnl"] = 0.0


def set_capital(capital):
    """Start a fresh account (wipes history). Use to (re)deposit a new balance."""
    if STATE["running"]:
        return "stop trading before resetting capital"
    persistence.reset(float(capital))
    return f"account set to Rs.{round(float(capital))}"


def withdraw(amount):
    """Fake-withdraw cash from the rolling balance; trading continues on the rest."""
    acct = persistence.load()
    amt = float(amount)
    if amt <= 0 or amt > acct["capital"]:
        return f"invalid amount (balance Rs.{round(acct['capital'])})"
    acct["capital"] -= amt
    acct["withdrawals"].insert(0, {"date": _now_ist().strftime("%Y-%m-%d %H:%M"), "amount": amt})
    persistence.save(acct)
    if STATE["running"]:
        STATE["capital"] -= amt          # reflect live so position sizing uses it
    _log(f"WITHDRAW Rs.{round(amt)} — balance now Rs.{round(acct['capital'])}")
    return f"withdrew Rs.{round(amt)}; balance Rs.{round(acct['capital'])}"


def start(capital=None, interval="3", ignore_hours=False, auto=False):
    """Resume the persisted (carried-over) balance and begin trading.
    If no account exists yet, initialise it from `capital`."""
    global _thread
    if STATE["running"]:
        return "already running"
    acct = persistence.load()
    if acct["capital"] > 0:
        cap = acct["capital"]                       # carry over earned balance
    elif capital and float(capital) > 0:
        acct = persistence.reset(float(capital))     # first-time deposit
        cap = float(capital)
    else:
        return "no balance — set starting capital first"
    try:
        exps = client.expiry_list(config.UNDER_SCRIP, "IDX_I")
    except Exception as e:
        return f"expiry fetch failed: {e}"
    STATE.update(running=True, capital=cap, start_capital=cap, day_start_capital=cap,
                 day_wins=0, open=[], closed=[], log=[], last_bar=None, auto=bool(auto),
                 trades_today=0, day_pnl=0.0, consec_losses=0, halted=False,
                 trade_day=_now_ist().date().isoformat(),
                 expiry=exps[0] if exps else None,
                 params={"interval": str(interval), "ignore_hours": ignore_hours})
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    threading.Thread(target=_tick_loop, daemon=True).start()   # fast live ticking
    _log(f"PAPER START{' [AUTO]' if auto else ''} — balance Rs.{round(cap)}, expiry {STATE['expiry']}")
    return "started"


_sched_thread = None
_sched_stop = threading.Event()
AUTO_START = dtime(9, 15)
AUTO_STOP = dtime(15, 15)


def start_scheduler():
    """Background clock: auto-start at 9:15 IST and auto-stop at 15:15, weekdays.
    Only acts if an account balance exists. Safe to call once at app startup."""
    global _sched_thread
    if _sched_thread and _sched_thread.is_alive():
        return
    _sched_stop.clear()
    _sched_thread = threading.Thread(target=_sched_loop, daemon=True)
    _sched_thread.start()


def _sched_loop():
    while not _sched_stop.is_set():
        try:
            n = _now_ist()
            weekday = n.weekday() < 5
            in_session = AUTO_START <= n.time() < AUTO_STOP
            if weekday and in_session and not STATE["running"]:
                if persistence.load()["capital"] > 0:
                    start(auto=True)                 # resume carried-over balance
            elif STATE["running"] and STATE.get("auto") and n.time() >= AUTO_STOP:
                stop()                               # square-off shutdown
        except Exception as e:
            _log(f"scheduler error: {e}")
        _sched_stop.wait(20)


def _finalize_day():
    """Record the day's result to the persistent account (one row per day)."""
    acct = persistence.load()
    acct["capital"] = round(STATE["capital"], 2)
    day = STATE.get("trade_day") or _now_ist().date().isoformat()
    if STATE["closed"] or STATE["day_pnl"]:
        acct["daily"].insert(0, {
            "date": day, "start": round(STATE["day_start_capital"], 2),
            "end": round(STATE["capital"], 2), "pnl": round(STATE["day_pnl"], 2),
            "trades": len(STATE["closed"]), "wins": STATE["day_wins"],
        })
    persistence.save(acct)


def stop():
    _stop.set()
    if STATE["running"]:
        _finalize_day()
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
        _stop.wait(10)          # 10s strategy cycle (entries on new candle; exits run on 2s tick)
    STATE["running"] = False


def _tick_loop():
    """Fast loop — live ticking spot + open-position LTP every 2s (display)."""
    while not _stop.is_set():
        try:
            secs = {"IDX_I": [config.UNDER_SCRIP]}
            fno = [int(p["security_id"]) for p in STATE["open"] if p.get("security_id")]
            if fno:
                secs["NSE_FNO"] = fno
            q = client.ltp_quote(secs)
            sp = q.get("IDX_I", {}).get(str(config.UNDER_SCRIP))
            if sp:
                STATE["spot"] = sp
            for p in STATE["open"]:
                if p.get("security_id"):
                    lt = q.get("NSE_FNO", {}).get(str(p["security_id"]))
                    if lt:
                        p["cur_ltp"] = lt
                        p["upnl"] = round((lt - p["entry_ltp"]) * p["qty"], 2)
            _monitor()      # FAST exits — target/stop/breakeven/S&R checked every 2s
        except Exception:
            pass
        _stop.wait(2)


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

    # cache candles for the fast exit loop
    STATE["_df"] = df
    STATE["adx"] = adx_value(df)
    # NOTE: exits are handled in the fast 2s tick loop for speed

    if bar_time != STATE["last_bar"]:
        STATE["last_bar"] = bar_time
        side, mode, regime = engine.decide_entry(last, df, STATE["adx"])
        STATE["regime"] = regime
        if side:
            _on_signal(side, STATE["spot"], chain, mode)


def _on_signal(signal, spot, chain, mode="TREND"):
    want = "CE" if signal == "BUY" else "PE"
    for pos in list(STATE["open"]):
        if pos["opt_type"] != want:
            _exit(pos, "OPPOSITE")
    if any(p["opt_type"] == want for p in STATE["open"]):
        return
    _check_day()
    ok_gate, gmsg = engine.entry_gate(STATE["_df"], signal, mode, _now_ist().time())
    if not ok_gate:
        return _log(gmsg)
    ok_risk, rmsg = engine.can_trade(STATE["trades_today"], STATE["consec_losses"], STATE["day_pnl"])
    if not ok_risk:
        STATE["halted"] = "halted" in rmsg
        return _log(rmsg)
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
    lots = engine.size_lots(STATE["capital"], ltp)     # risk-based sizing (shared engine)
    if lots < 1:
        return _log(f"capital Rs.{round(STATE['capital'])} < 1 lot (Rs.{round(cost_lot)}); skip")
    qty = lots * config.LOT_SIZE
    try:
        sid = client.option_security_id("NIFTY", STATE["expiry"], opt["strike"], opt["type"])
    except Exception:
        sid = None
    STATE["open"].append({
        "signal": signal, "opt_type": opt["type"], "strike": opt["strike"],
        "security_id": sid, "lots": lots, "qty": qty, "entry_ltp": ltp, "entry_spot": spot,
        "delta": round(opt["delta"], 3), "peak_fav": 0.0, "mode": mode,
        "entry_time": _now_ist().strftime("%H:%M:%S"),
    })
    STATE["trades_today"] += 1
    _log(f"ENTRY [{mode}] {signal} {opt['type']} {opt['strike']} x{lots}lot @Rs.{ltp} "
         f"(cost Rs.{round(cost_lot*lots)}, delta {round(opt['delta'],2)})")


def _monitor():
    """Fast exit check — runs every 2s. Targets/stops on the REAL PREMIUM."""
    eod = _now_ist().time() >= dtime(15, 15)
    df = STATE.get("_df")
    for pos in list(STATE["open"]):
        prem = pos.get("cur_ltp") or pos["entry_ltp"]
        prem_pct = (prem - pos["entry_ltp"]) / pos["entry_ltp"] * 100
        pos["prem_pct"] = round(prem_pct, 1)
        pos["peak_prem"] = max(pos.get("peak_prem", 0.0), prem_pct)
        should_exit, reason = engine.decide_exit(
            df, pos["opt_type"], pos["entry_ltp"], prem, pos["peak_prem"], eod)
        if should_exit:
            _exit(pos, reason)


def _exit(pos, reason):
    ltp = pos.get("cur_ltp") or pos["entry_ltp"]
    if ltp <= 0:
        ltp = pos["entry_ltp"]
    pnl = (ltp - pos["entry_ltp"]) * pos["qty"]
    bv, sv = pos["entry_ltp"] * pos["qty"], ltp * pos["qty"]
    charges = 40 + 0.001 * sv + 0.0003503 * (bv + sv) + 0.00003 * bv + 0.18 * (40 + 0.0003503 * (bv + sv))
    pnl -= charges
    STATE["capital"] += pnl
    STATE["day_pnl"] += pnl
    STATE["consec_losses"] = STATE["consec_losses"] + 1 if pnl < 0 else 0
    if pnl > 0:
        STATE["day_wins"] += 1
    pos.update(exit_ltp=ltp, exit_reason=reason, pnl=round(pnl, 2),
               exit_time=_now_ist().strftime("%H:%M:%S"))
    STATE["open"].remove(pos)
    STATE["closed"].insert(0, pos)
    # persist the trade + rolling balance immediately (survives restart)
    try:
        acct = persistence.load()
        acct["capital"] = round(STATE["capital"], 2)
        acct["trades"].insert(0, {
            "date": STATE.get("trade_day"), "time": pos["exit_time"],
            "side": pos["signal"], "opt_type": pos["opt_type"], "strike": pos["strike"],
            "entry": pos["entry_ltp"], "exit": round(ltp, 2),
            "pnl": round(pnl, 2), "reason": reason,
        })
        acct["trades"] = acct["trades"][:1000]
        persistence.save(acct)
    except Exception:
        pass
    _log(f"EXIT [{reason}] {pos['opt_type']} {pos['strike']} @Rs.{ltp} "
         f"P&L Rs.{round(pnl)} | capital Rs.{round(STATE['capital'])}")


def state_json():
    closed = STATE["closed"]
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    realized = STATE["capital"] - STATE["start_capital"]
    upnl = sum(p.get("upnl", 0) for p in STATE["open"])
    return {
        "running": STATE["running"], "capital": round(STATE["capital"], 2),
        "start_capital": STATE["start_capital"], "spot": STATE["spot"],
        "expiry": STATE["expiry"], "trades_today": STATE["trades_today"],
        "pnl": round(realized, 2), "unrealized": round(upnl, 2),
        "total_pnl": round(realized + upnl, 2),
        "closed_count": len(closed), "wins": wins,
        "win_rate": round(100 * wins / len(closed), 1) if closed else 0,
        "halted": STATE["halted"], "consec_losses": STATE["consec_losses"],
        "open": STATE["open"], "closed": closed[:50], "log": STATE["log"],
        "sr": _get_sr(), "auto": STATE.get("auto", False),
    }


def account_state():
    """Persisted account: rolling balance, withdrawals, and per-day history."""
    acct = persistence.load()
    daily = acct.get("daily", [])
    total_pnl = round(sum(d.get("pnl", 0) for d in daily), 2)
    total_wd = round(sum(w.get("amount", 0) for w in acct.get("withdrawals", [])), 2)
    return {
        "capital": round(acct.get("capital", 0), 2),
        "initial": round(acct.get("initial", 0), 2),
        "total_pnl": total_pnl,
        "total_withdrawn": total_wd,
        "days_traded": len(daily),
        "daily": daily[:120],
        "withdrawals": acct.get("withdrawals", [])[:120],
        "trades": acct.get("trades", [])[:300],
        "live": STATE["running"],
    }


def _get_sr():
    df = STATE.get("_df")
    if df is None or len(df) < 20:
        return {}
    r, s = engine.recent_sr(df)      # RECENT-window S&R (shared engine)
    return {"resistances": [round(x, 1) for x in r],
            "supports": [round(x, 1) for x in s],
            "regime": STATE.get("regime"), "adx": STATE.get("adx"), "warnings": []}
