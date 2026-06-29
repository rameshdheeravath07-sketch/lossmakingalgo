"""
FastAPI backend + dashboard for the VWAP-cross intraday options backtester.

Run:  uvicorn app:app --reload
Open: http://127.0.0.1:8000
"""
from __future__ import annotations
import io, os, re, json, asyncio
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
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
        "vwap_points": f"{config.VWAP_POINTS_EMA9}/{config.VWAP_POINTS_EMA15}",
        "premium_target": config.PREMIUM_TARGET_PCT, "premium_stop": config.PREMIUM_STOP_PCT,
        "premium_be": config.PREMIUM_BE_PCT, "premium_trail": config.PREMIUM_TRAIL_PCT,
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


@app.get("/api/paper/stream")
async def paper_stream():
    """SSE stream — pushes paper state to browser every 2s, no page refresh."""
    async def gen():
        while True:
            data = json.dumps(paper_trader.state_json())
            yield f"data: {data}\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


@app.get("/api/real/funds")
def real_funds():
    try:
        f = client.get_funds()
        return {k: v for k, v in f.items() if k != "raw"}
    except Exception as e:
        return {"error": str(e)}


# ---- Settings: manage Dhan token from the UI ----
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def _update_env(updates: dict):
    lines = open(ENV_PATH).read().splitlines() if os.path.exists(ENV_PATH) else []
    seen, out = set(), []
    for ln in lines:
        m = re.match(r"^(\w+)=", ln)
        if m and m.group(1) in updates:
            out.append(f"{m.group(1)}={updates[m.group(1)]}"); seen.add(m.group(1))
        else:
            out.append(ln)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    open(ENV_PATH, "w").write("\n".join(out) + "\n")


class Creds(BaseModel):
    client_id: str = ""
    access_token: str = ""


@app.get("/api/settings")
def get_settings():
    tok = config.DHAN_ACCESS_TOKEN
    return {"client_id": config.DHAN_CLIENT_ID, "token_set": bool(tok),
            "token_masked": (tok[:6] + "…" + tok[-4:]) if tok and len(tok) > 12 else ("set" if tok else "")}


@app.post("/api/settings")
def save_settings(c: Creds):
    if c.client_id.strip():
        config.DHAN_CLIENT_ID = c.client_id.strip()
    if c.access_token.strip():
        config.DHAN_ACCESS_TOKEN = c.access_token.strip()
    _update_env({"DHAN_CLIENT_ID": config.DHAN_CLIENT_ID, "DHAN_ACCESS_TOKEN": config.DHAN_ACCESS_TOKEN})
    return {"saved": True}


@app.post("/api/settings/test")
def test_settings():
    try:
        exps = client.expiry_list(config.UNDER_SCRIP, "IDX_I")
        return {"ok": True, "msg": f"Connected ✓  nearest expiry {exps[0] if exps else '?'}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


app.mount("/", StaticFiles(directory="static", html=True), name="static")

