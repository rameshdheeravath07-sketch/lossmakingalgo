"""
FastAPI backend + dashboard for the VWAP-cross intraday options backtester.

Run:  uvicorn app:app --reload
Open: http://127.0.0.1:8000
"""
from __future__ import annotations
import io
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import config
from strategy import get_signals
from backtest import run_backtest, sweep_vwap_threshold, sweep_target, walk_forward
from dhan_client import client
import paper_trader
import real_trader

app = FastAPI(title="VWAP-Cross Options Backtester")


class DhanRequest(BaseModel):
    security_id: str = "13"           # NIFTY index
    exchange_segment: str = "IDX_I"
    instrument_type: str = "INDEX"
    interval: str = "3"
    days: int = 90
    option_delta: float = 0.6
    capital: float = 15000


class InspectRequest(DhanRequest):
    at: str = None
    window: int = 6


def _fetch(req: DhanRequest) -> pd.DataFrame:
    try:
        df = client.historical_candles(
            security_id=req.security_id, exchange_segment=req.exchange_segment,
            instrument_type=req.instrument_type, interval=req.interval, days=req.days)
    except Exception as e:
        raise HTTPException(400, f"Dhan fetch failed: {e}")
    if len(df) < 50:
        raise HTTPException(400, f"Only {len(df)} candles — widen the date range.")
    return df


def _trades(res):
    return [{"entry_time": str(t.entry_time), "side": t.side, "entry": t.entry_price,
             "exit_time": str(t.exit_time), "exit": t.exit_price,
             "reason": t.exit_reason, "pnl": t.pnl_rupees} for t in res.trades]


@app.get("/api/config")
def get_config():
    return {
        "vwap_points": config.VWAP_CROSS_POINTS, "target_pct": config.PROFIT_TARGET_PCT,
        "stop_pct": config.HARD_STOP_PCT, "breakeven_pct": config.BREAKEVEN_TRIGGER_PCT,
        "capital": config.CAPITAL_PER_TRADE, "max_trades_day": config.MAX_TRADES_PER_DAY,
        "square_off": config.INTRADAY_ONLY,
    }


@app.post("/api/backtest-dhan")
def backtest_dhan(req: DhanRequest):
    df = _fetch(req)
    res = run_backtest(df, option_delta=req.option_delta, capital=req.capital)
    return {"candles": len(df), "from": str(df.index[0]), "to": str(df.index[-1]),
            "summary": res.summary(), "trades": _trades(res)}


@app.post("/api/sweep-vwap")
def sweep_vwap(req: DhanRequest):
    df = _fetch(req)
    return {"results": sweep_vwap_threshold(df, capital=req.capital, option_delta=req.option_delta)}


@app.post("/api/sweep-target")
def sweep_target_ep(req: DhanRequest):
    df = _fetch(req)
    return {"results": sweep_target(df, capital=req.capital, option_delta=req.option_delta)}


@app.post("/api/walkforward")
def walkforward(req: DhanRequest):
    df = _fetch(req)
    if len(df) < 100:
        raise HTTPException(400, "Need ~90 days for a valid train/test split.")
    return walk_forward(df, capital=req.capital, option_delta=req.option_delta)


@app.post("/api/inspect-dhan")
def inspect_dhan(req: InspectRequest):
    df = _fetch(req)
    out = get_signals(df)
    cols = [c for c in ["close", "ema9", "ema15", "vwap", "signal"] if c in out.columns]
    idx = out.index.get_indexer([pd.to_datetime(req.at)], method="nearest")[0] if req.at else len(out) - 1
    lo, hi = max(0, idx - req.window), min(len(out), idx + req.window + 1)
    rows = out.iloc[lo:hi][cols].round(2)
    return {"center": str(out.index[idx]), "rows": rows.reset_index().astype(str).to_dict("records")}


@app.post("/api/backtest-csv")
async def backtest_csv(file: UploadFile = File(...), option_delta: float = 0.6, capital: float = 15000):
    df = pd.read_csv(io.BytesIO(await file.read()))
    df.columns = [c.lower().strip() for c in df.columns]
    if not {"open", "high", "low", "close"}.issubset(df.columns):
        raise HTTPException(400, "CSV needs open,high,low,close")
    if "volume" not in df.columns:
        df["volume"] = 0
    for c in ("timestamp", "date"):
        if c in df.columns:
            df.index = pd.to_datetime(df[c]); break
    res = run_backtest(df, option_delta=option_delta, capital=capital)
    return {"summary": res.summary(), "trades": _trades(res)}


class PaperStart(BaseModel):
    capital: float = 15000
    interval: str = "3"
    ignore_hours: bool = False     # set true to test outside market hours


@app.post("/api/paper/start")
def paper_start(req: PaperStart):
    return {"result": paper_trader.start(req.capital, req.interval, req.ignore_hours)}


@app.post("/api/paper/stop")
def paper_stop():
    return {"result": paper_trader.stop()}


@app.get("/api/paper/state")
def paper_state():
    return paper_trader.state_json()


# ---- REAL trading (guarded) ----
class ArmReq(BaseModel):
    phrase: str = ""

class RealStart(BaseModel):
    lots: int = 1
    interval: str = "3"


@app.post("/api/real/arm")
def real_arm(req: ArmReq):
    return {"armed": real_trader.arm(req.phrase)}


@app.post("/api/real/start")
def real_start(req: RealStart):
    return {"result": real_trader.start(req.lots, req.interval)}


@app.post("/api/real/stop")
def real_stop():
    return {"result": real_trader.stop()}


@app.get("/api/real/state")
def real_state():
    return real_trader.state_json()


app.mount("/", StaticFiles(directory="static", html=True), name="static")

