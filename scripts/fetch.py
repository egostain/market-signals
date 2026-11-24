"""Collect market indicators and write data/latest.json.

Sources
- FRED API (needs FRED_API_KEY secret)
- Yahoo Finance via yfinance (approximate)
- KRX foreign net buying on KOSPI via pykrx, Naver Finance fallback

Every block is isolated: a failure is recorded in "errors" and the rest still runs.
"""
from __future__ import annotations

import io
import json
import os
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

OUT = Path(__file__).resolve().parent.parent / "data" / "latest.json"
UA = {"User-Agent": "Mozilla/5.0 (market-signals bot)"}
NOW = datetime.now(timezone.utc)
START = (NOW - timedelta(days=420)).date()

FRED_SERIES = [
    "DGS10", "DGS2", "DFEDTARU", "BAMLH0A0HYM2", "DEXJPUS", "DEXKOUS",
    "UNRATE", "SAHMREALTIME", "IC4WSA", "NFCI", "VIXCLS", "SP500",
]

result: dict = {
    "generated_at_utc": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "fred": {},
    "yahoo": {},
    "krx": {},
    "derived": {},
    "errors": [],
}


def err(key: str, e: Exception) -> None:
    result["errors"].append({"item": key, "error": f"{type(e).__name__}: {e}"})
    traceback.print_exc()


def last_n(s: pd.Series, n: int) -> list:
    s = s.dropna().tail(n)
    return [{"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 4)} for d, v in s.items()]


def streak(s: pd.Series, cond) -> int:
    """Count consecutive most-recent observations satisfying cond."""
    n = 0
    for v in reversed(s.dropna().tolist()):
        if cond(v):
            n += 1
        else:
            break
    return n


def pct_change_over(s: pd.Series, days: int) -> float | None:
    s = s.dropna()
    if s.empty:
        return None
    end = s.index[-1]
    past = s[s.index <= end - pd.Timedelta(days=days)]
    if past.empty:
        return None
    return round((s.iloc[-1] / past.iloc[-1] - 1) * 100, 2)


# ---------------------------------------------------------------- FRED
fred: dict[str, pd.Series] = {}
key = os.environ.get("FRED_API_KEY")
for sid in FRED_SERIES:
    try:
        if not key:
            raise RuntimeError("FRED_API_KEY not set")
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": sid, "api_key": key, "file_type": "json",
                    "observation_start": START.isoformat()},
            timeout=30,
        )
        r.raise_for_status()
        obs = r.json()["observations"]
        s = pd.Series(
            {pd.Timestamp(o["date"]): float(o["value"]) for o in obs if o["value"] not in (".", "")}
        ).sort_index()
        if s.empty:
            raise RuntimeError("no observations")
        fred[sid] = s
        result["fred"][sid] = {
            "latest": last_n(s, 1)[0],
            "recent": last_n(s, 10),
        }
    except Exception as e:  # noqa: BLE001
        err(f"fred:{sid}", e)

# ---------------------------------------------------------------- Yahoo
yahoo: dict[str, pd.Series] = {}
try:
    import yfinance as yf

    tickers = {"sp500": "^GSPC", "vix_yahoo": "^VIX", "kospi": "^KS11",
               "usdkrw_yahoo": "KRW=X", "usdjpy_yahoo": "JPY=X"}
    for name, t in tickers.items():
        try:
            h = yf.Ticker(t).history(period="2y", auto_adjust=False)
            s = h["Close"].dropna()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            if s.empty:
                raise RuntimeError("empty history")
            yahoo[name] = s
            entry = {"latest": last_n(s, 1)[0], "recent": last_n(s, 10)}
            if name in ("sp500", "kospi"):
                ma50 = s.rolling(50).mean()
                ma200 = s.rolling(200).mean()
                entry["ma50"] = round(float(ma50.iloc[-1]), 2)
                entry["ma200"] = round(float(ma200.iloc[-1]), 2)
                entry["pct_vs_ma200"] = round((s.iloc[-1] / ma200.iloc[-1] - 1) * 100, 2)
                entry["days_below_ma200"] = streak(s - ma200, lambda v: v < 0)
                entry["days_above_ma200"] = streak(s - ma200, lambda v: v > 0)
                entry["death_cross"] = bool(ma50.iloc[-1] < ma200.iloc[-1])
            if name in ("usdkrw_yahoo", "usdjpy_yahoo"):
                entry["chg_1m_pct"] = pct_change_over(s, 30)
            yahoo[name] = s
            result["yahoo"][name] = entry
        except Exception as e:  # noqa: BLE001
            err(f"yahoo:{name}", e)
except Exception as e:  # noqa: BLE001
    err("yahoo:import", e)

# S&P 500 breadth: share of constituents above their 200-day MA
try:
    import yfinance as yf

    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                        headers=UA, timeout=30).text
    syms = pd.read_html(io.StringIO(html))[0]["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
    px = yf.download(syms, period="15mo", auto_adjust=True, progress=False, threads=True)["Close"]
    px = px.dropna(axis=1, thresh=200)
    ma = px.rolling(200).mean()
    last = px.ffill().iloc[-1]
    above = (last > ma.iloc[-1]).sum()
    total = int(ma.iloc[-1].notna().sum())
    result["yahoo"]["sp500_pct_above_200dma"] = {
        "date": px.index[-1].strftime("%Y-%m-%d"),
        "value": round(float(above) / total * 100, 1),
        "n_above": int(above), "n_total": total,
    }
except Exception as e:  # noqa: BLE001
    err("yahoo:sp500_pct_above_200dma", e)

# ---------------------------------------------------------------- KRX foreign flows
def krx_pykrx() -> pd.Series:
    from pykrx import stock

    end = NOW.astimezone(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    beg = (NOW - timedelta(days=45)).strftime("%Y%m%d")
    df = stock.get_market_trading_value_by_date(beg, end, "KOSPI")
    col = [c for c in df.columns if "외국인" in c][0]
    s = (df[col] / 1e8).astype(float)  # KRW -> 억원
    s.index = pd.to_datetime(s.index)
    if s.empty:
        raise RuntimeError("pykrx returned empty frame")
    return s


def krx_naver() -> pd.Series:
    rows = []
    bizdate = NOW.astimezone(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    for page in (1, 2, 3):
        r = requests.get("https://finance.naver.com/sise/investorDealTrendDay.naver",
                         params={"bizdate": bizdate, "sosok": "01", "page": page},
                         headers=UA, timeout=30)
        r.encoding = "euc-kr"
        t = pd.read_html(io.StringIO(r.text))[0].dropna(how="all")
        rows.append(t)
    df = pd.concat(rows)
    df.columns = [c if isinstance(c, str) else c[-1] for c in df.columns]
    date_col, frn_col = df.columns[0], [c for c in df.columns if "외국인" in str(c)][0]
    df = df[[date_col, frn_col]].dropna()
    df = df[df[date_col].astype(str).str.match(r"\d{2}\.\d{2}\.\d{2}")]
    s = pd.Series(df[frn_col].astype(float).values,
                  index=pd.to_datetime(df[date_col], format="%y.%m.%d")).sort_index()
    if s.empty:
        raise RuntimeError("naver returned no rows")
    return s[~s.index.duplicated()]


frn = None
for name, fn in (("pykrx", krx_pykrx), ("naver", krx_naver)):
    try:
        frn = fn()
        result["krx"]["source"] = name
        break
    except Exception as e:  # noqa: BLE001
        err(f"krx:{name}", e)
if frn is not None:
    result["krx"]["kospi_foreign_net"] = {
        "unit": "억원",
        "latest": last_n(frn, 1)[0],
        "recent": last_n(frn, 10),
        "consecutive_net_buy_days": streak(frn, lambda v: v > 0),
        "consecutive_net_sell_days": streak(frn, lambda v: v < 0),
        "sum_5d": round(float(frn.tail(5).sum()), 1),
        "sum_20d": round(float(frn.tail(20).sum()), 1),
    }

# ---------------------------------------------------------------- Derived checks
d = result["derived"]


def safe(name, fn):
    try:
        d[name] = fn()
    except Exception as e:  # noqa: BLE001
        err(f"derived:{name}", e)


if "DGS10" in fred:
    safe("dgs10_days_above_5", lambda: streak(fred["DGS10"], lambda v: v > 5.0))

    def last_run_above_5():
        s = fred["DGS10"].dropna()
        above = s > 5.0
        if not above.any():
            return None
        end = above[above].index[-1]
        run = streak(s[:end], lambda v: v > 5.0)
        return {"end_date": end.strftime("%Y-%m-%d"), "days": run}
    safe("dgs10_last_run_above_5", last_run_above_5)
if "DGS2" in fred and "DFEDTARU" in fred:
    safe("dgs2_minus_fedupper", lambda: round(float(fred["DGS2"].iloc[-1] - fred["DFEDTARU"].iloc[-1]), 3))
if "BAMLH0A0HYM2" in fred:
    def hy():
        s = fred["BAMLH0A0HYM2"]
        w = s[s.index >= s.index[-1] - pd.Timedelta(days=182)]
        lo, hi = w.idxmin(), w.idxmax()
        return {
            "latest": float(s.iloc[-1]),
            "min_6m": {"date": lo.strftime("%Y-%m-%d"), "value": float(w[lo])},
            "max_6m": {"date": hi.strftime("%Y-%m-%d"), "value": float(w[hi])},
            "vs_min_6m_pp": round(float(s.iloc[-1] - w.min()), 2),
            "vs_max_6m_pp": round(float(s.iloc[-1] - w.max()), 2),
        }
    safe("hy_spread", hy)
if "DEXJPUS" in fred:
    # USD/JPY up = yen weaker. Yen appreciation % = -(USDJPY change)
    safe("yen_appreciation_1m_pct", lambda: -pct_change_over(fred["DEXJPUS"], 30))
if "DEXKOUS" in fred:
    safe("usdkrw_chg_1m_pct", lambda: pct_change_over(fred["DEXKOUS"], 30))
if "UNRATE" in fred:
    safe("unrate_last2", lambda: last_n(fred["UNRATE"], 2))
vix = fred.get("VIXCLS", yahoo.get("vix_yahoo"))
if vix is not None:
    safe("vix_days_ge_25", lambda: streak(vix, lambda v: v >= 25))
    safe("vix_days_ge_30", lambda: streak(vix, lambda v: v >= 30))
    safe("vix_days_lt_25", lambda: streak(vix, lambda v: v < 25))

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
print(f"wrote {OUT} with {len(result['errors'])} errors")
