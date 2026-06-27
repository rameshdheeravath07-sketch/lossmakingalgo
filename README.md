# VWAP-Cross Intraday Options Backtester

A clean, validated backtester for one strategy that works on NIFTY.

## The strategy
- **Entry:** both the 9 EMA and 15 EMA are ≥ `VWAP_CROSS_POINTS` away from VWAP
  - both **above** VWAP → buy **CE**
  - both **below** VWAP → buy **PE**
- **Exits:** profit target, break-even stop, hard stop, opposite signal, 3:15 PM square-off
- **Costs:** realistic Indian F&O charges (STT, GST, exchange, stamp, brokerage) + slippage

## Validated result (NIFTY, 3-min, 90 days, ₹15k)
- ~52-59% win, profit factor ~1.7, +₹21,800 net **after all charges**
- **Passed the out-of-sample blind test (ROBUST)**
- Max drawdown ≈ ₹16k → size your account accordingly

## Setup
```
pip install -r requirements.txt
# put DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN in .env
uvicorn app:app --reload      # open http://127.0.0.1:8000
```

## Dashboard buttons
- **Fetch from Dhan & Backtest** — run the strategy on live-fetched history
- **VWAP Threshold Sweep** — find the best distance
- **Profit-Target Sweep** — see win-rate / profit trade-off
- **Out-of-Sample Test** — train on 60%, verify blind on 40% (is the edge real?)
- **Inspect that bar** — see ema9/ema15/vwap/signal at any bar

## Files
| File | Purpose |
|------|---------|
| `strategy.py` | VWAP-cross signal logic |
| `backtest.py` | backtester + sweeps + out-of-sample |
| `dhan_client.py` | fetch historical candles from Dhan |
| `app.py` | FastAPI backend + dashboard |
| `config.py` | all settings (overridable via .env) |
| `static/index.html` | dashboard UI |

## Important
- Backtest only. **Not validated on live fills** — paper-trade before real money.
- Defaults: NIFTY (security_id 13), interval 3, delta 0.6, ₹15k. Set them in `.env`.
- Worst-case drawdown ≈ ₹16k on ₹15k/trade → keep per-trade size ≤ ~10% of total account.
