"""
Thin wrapper around dhanhq for:
  - historical/intraday candles (for signals + backtest)
  - option-chain lookup to pick an ITM strike
  - placing option orders (guarded by LIVE_TRADING)

Docs: https://dhanhq.co/docs/v2/
This module degrades gracefully: if credentials are missing it raises only
when you actually try to call the network.
"""
from __future__ import annotations
import datetime as dt
import pandas as pd
import requests

from config import config

DHAN_API = "https://api.dhan.co/v2"

try:
    from dhanhq import dhanhq
except Exception:  # library optional until installed
    dhanhq = None


class DhanClient:
    def __init__(self):
        self._dhan = None

    @property
    def dhan(self):
        if self._dhan is None:
            if dhanhq is None:
                raise RuntimeError("dhanhq not installed. pip install dhanhq")
            if not config.DHAN_CLIENT_ID or not config.DHAN_ACCESS_TOKEN:
                raise RuntimeError("Dhan credentials missing. Set them in .env")
            self._dhan = dhanhq(config.DHAN_CLIENT_ID, config.DHAN_ACCESS_TOKEN)
        return self._dhan

    # ------------------------------------------------------------------
    # market data
    # ------------------------------------------------------------------
    def intraday_candles(self, security_id: str, exchange_segment: str,
                         interval: str = "5", days: int = 5) -> pd.DataFrame:
        """Return OHLCV DataFrame indexed by datetime."""
        to_d = dt.date.today()
        from_d = to_d - dt.timedelta(days=days)
        resp = self.dhan.intraday_minute_data(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type="INDEX",
            from_date=str(from_d),
            to_date=str(to_d),
            interval=interval,
        )
        data = resp.get("data", resp)
        df = pd.DataFrame({
            "open": data["open"],
            "high": data["high"],
            "low": data["low"],
            "close": data["close"],
            "volume": data.get("volume", [0] * len(data["open"])),
        })
        if "timestamp" in data:
            df.index = pd.to_datetime(data["timestamp"], unit="s")
        return df

    # Dhan caps each intraday request at ~90 days; chunk longer ranges.
    INTRADAY_CHUNK_DAYS = 90

    def _fetch_window(self, url, payload) -> pd.DataFrame:
        """Single Dhan charts request -> raw OHLCV DataFrame (no resample)."""
        r = requests.post(url, json=payload, headers=self._headers(), timeout=60)
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Dhan HTTP {r.status_code}: {r.text[:200]}")
        if isinstance(data, dict) and data.get("status") == "failure":
            raise RuntimeError(f"Dhan error: {data.get('remarks', data)}")
        if not isinstance(data, dict) or "open" not in data:
            raise RuntimeError(f"Dhan returned no data: {data}")
        df = pd.DataFrame({
            "open": data["open"], "high": data["high"], "low": data["low"],
            "close": data["close"],
            "volume": data.get("volume", [0] * len(data["open"])),
        })
        ts = data.get("timestamp") or data.get("start_Time")
        if ts is not None:
            df.index = pd.to_datetime(ts, unit="s")
        return df

    def historical_candles(self, security_id: str, exchange_segment: str,
                           instrument_type: str = "INDEX", interval: str = "5",
                           from_date: str = None, to_date: str = None,
                           days: int = 90) -> pd.DataFrame:
        """
        Historical OHLCV for backtesting. interval 'D' = daily, else minutes
        ('1','5','15','25','60'). Daily goes back years in one shot; intraday is
        capped per request (~90 days) by Dhan, so longer ranges (e.g. 1.5 years)
        are fetched in 90-day chunks and stitched together. Returns DataFrame
        indexed by datetime.
        """
        to_d = dt.date.fromisoformat(to_date) if to_date else dt.date.today()
        from_d = (dt.date.fromisoformat(from_date) if from_date
                  else to_d - dt.timedelta(days=days))

        interval = str(interval).strip().upper()
        ALLOWED = {"1", "5", "15", "25", "60"}
        resample_to = None        # minutes to resample to, if not natively supported
        alias = {"1D": "D", "DAY": "D", "DAILY": "D"}
        interval = alias.get(interval, interval)
        if interval != "D" and interval not in ALLOWED:
            # custom interval (e.g. 3, 10, 30) -> fetch 1-min and resample
            try:
                resample_to = int(interval)
                interval = "1"
            except ValueError:
                raise ValueError(
                    f"Invalid interval '{interval}'. Use a number of minutes "
                    f"or 'D' (daily).")

        if interval == "D":
            # daily history returns the whole range in one call
            url = f"{DHAN_API}/charts/historical"
            payload = {"securityId": str(security_id), "exchangeSegment": exchange_segment,
                       "instrument": instrument_type,
                       "fromDate": str(from_d), "toDate": str(to_d)}
            df = self._fetch_window(url, payload)
        else:
            # intraday: walk the range in <=90-day chunks and concatenate
            url = f"{DHAN_API}/charts/intraday"
            parts = []
            chunk = dt.timedelta(days=self.INTRADAY_CHUNK_DAYS)
            start = from_d
            while start <= to_d:
                end = min(start + chunk, to_d)
                payload = {"securityId": str(security_id), "exchangeSegment": exchange_segment,
                           "instrument": instrument_type, "interval": interval,
                           "fromDate": str(start), "toDate": str(end)}
                try:
                    part = self._fetch_window(url, payload)
                    if len(part):
                        parts.append(part)
                except RuntimeError:
                    pass  # skip empty/failed windows (e.g. holidays-only spans)
                start = end + dt.timedelta(days=1)
            if not parts:
                raise RuntimeError("Dhan returned no intraday data for the range")
            df = pd.concat(parts)
            df = df[~df.index.duplicated(keep="first")].sort_index()

        if resample_to and resample_to > 1:
            df = (df.resample(f"{resample_to}min", label="left", closed="left")
                    .agg({"open": "first", "high": "max", "low": "min",
                          "close": "last", "volume": "sum"})
                    .dropna())
        return df

    def _headers(self):
        return {"access-token": config.DHAN_ACCESS_TOKEN, "client-id": config.DHAN_CLIENT_ID,
                "Content-Type": "application/json", "Accept": "application/json"}

    def expiry_list(self, under_scrip: int = 13, under_seg: str = "IDX_I") -> list:
        r = requests.post(f"{DHAN_API}/optionchain/expirylist", headers=self._headers(),
                          json={"UnderlyingScrip": int(under_scrip), "UnderlyingSeg": under_seg},
                          timeout=20)
        data = r.json()
        if data.get("status") == "failure":
            raise RuntimeError(f"Dhan expiry error: {data.get('remarks', data)}")
        return data.get("data", [])

    def option_chain(self, expiry: str, under_scrip: int = 13, under_seg: str = "IDX_I") -> dict:
        """Live option chain via REST. NOTE: Dhan limits this to 1 request / 3 sec."""
        r = requests.post(f"{DHAN_API}/optionchain", headers=self._headers(),
                          json={"UnderlyingScrip": int(under_scrip), "UnderlyingSeg": under_seg,
                                "Expiry": expiry}, timeout=20)
        data = r.json()
        if data.get("status") == "failure":
            raise RuntimeError(f"Dhan optionchain error: {data.get('remarks', data)}")
        return data.get("data", data)

    @staticmethod
    def parse_chain(chain: dict):
        """-> (spot, {strike: {'ce':{ltp,delta}, 'pe':{ltp,delta}}})"""
        spot = chain.get("last_price")
        oc = chain.get("oc", {})
        rows = {}
        for k, legs in oc.items():
            try:
                strike = float(k)
            except (ValueError, TypeError):
                continue
            def leg(x):
                if not x:
                    return None
                g = x.get("greeks", {}) or {}
                return {"ltp": float(x.get("last_price") or 0),
                        "delta": float(g.get("delta") or 0)}
            rows[strike] = {"ce": leg(legs.get("ce")), "pe": leg(legs.get("pe"))}
        return (float(spot) if spot else None), rows

    def pick_itm(self, chain: dict, side: str, depth: int = 1):
        """Pick the depth-th ITM strike. side BUY->CE (below spot), SELL->PE (above)."""
        spot, rows = self.parse_chain(chain)
        if spot is None or not rows:
            raise RuntimeError("empty option chain")
        strikes = sorted(rows.keys())
        typ = "ce" if side == "BUY" else "pe"
        if typ == "ce":
            itm = sorted([s for s in strikes if s < spot], reverse=True)
        else:
            itm = sorted([s for s in strikes if s > spot])
        if not itm:
            raise RuntimeError("no ITM strike")
        strike = itm[min(depth - 1, len(itm) - 1)]
        leg = rows[strike][typ]
        return {"strike": strike, "type": typ.upper(), "ltp": leg["ltp"],
                "delta": leg["delta"], "spot": spot}

    def option_ltp(self, chain: dict, strike: float, opt_type: str) -> float:
        _, rows = self.parse_chain(chain)
        leg = rows.get(strike, {}).get(opt_type.lower())
        return leg["ltp"] if leg else 0.0

    def get_funds(self) -> dict:
        """Fetch Dhan account fund limit / available balance."""
        r = requests.get(f"{DHAN_API}/fundlimit", headers=self._headers(), timeout=15)
        d = r.json()
        if isinstance(d, dict) and d.get("status") == "failure":
            raise RuntimeError(d.get("remarks", d))
        return {
            "available": float(d.get("availabelBalance") or d.get("availableBalance") or 0),
            "sod_limit": float(d.get("sodLimit") or 0),
            "utilized": float(d.get("utilizedAmount") or 0),
            "collateral": float(d.get("collateralAmount") or 0),
            "raw": d,
        }

    def get_positions(self) -> list:
        """Fetch current open positions from Dhan."""
        r = requests.get(f"{DHAN_API}/positions", headers=self._headers(), timeout=15)
        d = r.json()
        return d.get("data", d) if isinstance(d, dict) else d

    def ltp_quote(self, securities: dict) -> dict:
        """Fast LTP for a list of securities (lighter than option chain).
        securities = {"IDX_I":[13], "NSE_FNO":[sid,...]} -> {seg:{sid:ltp}}."""
        body = {seg: [int(x) for x in ids] for seg, ids in securities.items() if ids}
        r = requests.post(f"{DHAN_API}/marketfeed/ltp", headers=self._headers(),
                          json=body, timeout=10)
        data = r.json()
        if data.get("status") == "failure":
            raise RuntimeError(data.get("remarks", data))
        out = {}
        for seg, d in (data.get("data", {}) or {}).items():
            out[seg] = {k: float(v.get("last_price") or 0) for k, v in d.items()}
        return out

    # ------------------------------------------------------------------
    # scrip master (maps strike -> security_id, needed for REAL orders)
    # ------------------------------------------------------------------
    _scrip = None

    def _load_scrip(self):
        if DhanClient._scrip is not None:
            return DhanClient._scrip
        url = "https://images.dhan.co/api-data/api-scrip-master.csv"
        df = pd.read_csv(url, low_memory=False)
        df.columns = [c.strip().upper() for c in df.columns]
        DhanClient._scrip = df
        return df

    def option_security_id(self, symbol, expiry, strike, opt_type):
        """Find the F&O security_id for a specific option contract."""
        df = self._load_scrip()
        def col(*names):
            for n in names:
                if n in df.columns:
                    return n
            return None
        c_sym = col("SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL")
        c_exp = col("SEM_EXPIRY_DATE")
        c_str = col("SEM_STRIKE_PRICE")
        c_opt = col("SEM_OPTION_TYPE")
        c_id = col("SEM_SMST_SECURITY_ID")
        c_seg = col("SEM_SEGMENT", "SEM_EXM_EXCH_ID")
        m = df[df[c_sym].astype(str).str.upper().str.startswith(symbol.upper())]
        m = m[m[c_opt].astype(str).str.upper() == opt_type.upper()]
        m = m[abs(m[c_str].astype(float) - float(strike)) < 0.01]
        m = m[m[c_exp].astype(str).str.startswith(str(expiry))]
        if len(m) == 0:
            raise RuntimeError(f"security_id not found for {symbol} {expiry} {strike} {opt_type}")
        return str(int(m.iloc[0][c_id]))

    def place_order(self, security_id, qty, side="BUY", exchange_segment="NSE_FNO",
                    product="INTRADAY", order_type="MARKET", price=0):
        """Place a REAL order via Dhan REST. Caller is responsible for arming."""
        body = {
            "dhanClientId": config.DHAN_CLIENT_ID,
            "transactionType": side, "exchangeSegment": exchange_segment,
            "productType": product, "orderType": order_type,
            "securityId": str(security_id), "quantity": int(qty),
            "price": price, "validity": "DAY",
        }
        r = requests.post(f"{DHAN_API}/orders", headers=self._headers(), json=body, timeout=20)
        return r.json()

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    def place_option_order(self, security_id: str, exchange_segment: str,
                           quantity: int, side: str = "BUY") -> dict:
        """side BUY/SELL. Always MARKET intraday here; adjust as needed."""
        if not config.LIVE_TRADING:
            return {"status": "SKIPPED_PAPER",
                    "security_id": security_id, "qty": quantity, "side": side}
        txn = self.dhan.BUY if side == "BUY" else self.dhan.SELL
        return self.dhan.place_order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type=txn,
            quantity=quantity,
            order_type=self.dhan.MARKET,
            product_type=self.dhan.INTRA,
            price=0,
        )

    def positions(self) -> dict:
        return self.dhan.get_positions()

    def ltp(self, security_id: str, exchange_segment: str = "NSE_FNO") -> float:
        """Last traded price for a single instrument."""
        if not config.LIVE_TRADING:
            # in paper mode the caller supplies a simulated price
            return 0.0
        try:
            resp = self.dhan.ohlc_data(
                securities={exchange_segment: [int(security_id)]}
            )
            data = resp.get("data", {}).get(exchange_segment, {})
            leg = data.get(str(security_id), {})
            return float(leg.get("last_price") or leg.get("ltp") or 0)
        except Exception:
            return 0.0


client = DhanClient()
