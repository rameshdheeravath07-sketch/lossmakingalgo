"""
Tiny JSON-file store for the PAPER-trading account so it survives restarts and
carries capital across days (compounding). Holds:
  - capital        : current rolling balance (carries over day to day)
  - initial        : the first deposit
  - withdrawals    : [{date, amount}]
  - daily          : [{date, start, end, pnl, trades, wins}]  one row per day
  - trades         : [{date, side, opt_type, strike, entry, exit, pnl, reason}]

NOTE: on Render's FREE plan the filesystem is ephemeral — this file resets on
deploy/sleep. For durable history use an always-on plan with a persistent disk
(set ACCOUNT_FILE to a path on that disk via env), or a real DB later.
"""
from __future__ import annotations
import json, os, threading

ACCOUNT_FILE = os.getenv("ACCOUNT_FILE",
                         os.path.join(os.path.dirname(__file__), "data", "paper_account.json"))
_lock = threading.Lock()

_DEFAULT = {"capital": 0.0, "initial": 0.0, "withdrawals": [],
            "daily": [], "trades": []}


def load() -> dict:
    with _lock:
        try:
            with open(ACCOUNT_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k, v in _DEFAULT.items():
                d.setdefault(k, v if not isinstance(v, list) else list(v))
            return d
        except (FileNotFoundError, json.JSONDecodeError):
            return {k: (list(v) if isinstance(v, list) else v) for k, v in _DEFAULT.items()}


def save(d: dict) -> None:
    with _lock:
        os.makedirs(os.path.dirname(ACCOUNT_FILE), exist_ok=True)
        tmp = ACCOUNT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, ACCOUNT_FILE)


def reset(capital: float) -> dict:
    """Start a brand-new account with a fresh deposit (wipes history)."""
    d = {"capital": float(capital), "initial": float(capital),
         "withdrawals": [], "daily": [], "trades": []}
    save(d)
    return d


def has_account() -> bool:
    return load()["capital"] > 0 or load()["initial"] > 0
