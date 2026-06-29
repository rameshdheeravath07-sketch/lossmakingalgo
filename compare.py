"""Quick comparison: mean-reversion OFF (skip sideways) vs ON. Run: python compare.py"""
import warnings; warnings.filterwarnings("ignore")
from dhan_client import client
from backtest import run_backtest, walk_forward
from config import config

df = client.historical_candles("13", "IDX_I", "INDEX", "3", days=90)
print(f"Data: {len(df)} candles  {df.index[0]} -> {df.index[-1]}\n")

for label, only_trend in [("SKIP sideways (trend only)", True),
                          ("MEAN-REVERSION in sideways", False)]:
    config.TRADE_ONLY_TRENDING = only_trend
    s = run_backtest(df, option_delta=0.6, capital=15000).summary()
    print(f"{label:>28}: trades={s['trades']:>4} win={s['win_rate']:>5}% "
          f"PF={s['profit_factor']} net=Rs.{round(s['net_pnl'])}")

print("\nOut-of-sample (trend-only):")
config.TRADE_ONLY_TRENDING = True
wf = walk_forward(df, capital=15000)
print(f"  TRAIN {wf['train_summary']}  ->  TEST {wf['test_summary']}  =>  {wf['verdict']}")
