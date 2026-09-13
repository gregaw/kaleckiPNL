"""The only module besides sync.py that talks to the network. Two calls, both carrying nothing
but public identifiers: an ISO currency code and a date to NBP; a list of tickers to Yahoo.
Position sizes, amounts and unit names never leave the machine.

The NBP endpoint shape was written from documentation, not exercised from the build
environment (api.nbp.pl was unreachable there); the first real call on the owner's machine
is the verification, and failures degrade to the sheet's own rate."""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlparse

import polars as pl

ALLOWED_HOSTS = {"api.nbp.pl", "query1.finance.yahoo.com", "query2.finance.yahoo.com"}
NBP_URL = "https://api.nbp.pl/api/exchangerates/rates/a/{ccy}/{date}/?format=json"
TICKER_RE = re.compile(r"^[A-Z0-9.\-=^]{1,20}$")
CCY_RE = re.compile(r"^[A-Z]{3}$")


class NetworkDisabled(RuntimeError):
    pass


def assert_allowed(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise NetworkDisabled(f"refusing request to {host!r}: not in the allowlist")


def nbp_rate(ccy: str, on: dt.date, offline: bool = False, session=None, max_back: int = 10) -> tuple[float, dt.date] | None:
    """Table A mid rate for `ccy` on `on`, walking back over non-trading days.
    Returns (rate, effective_date) or None."""
    ccy = ccy.upper()
    if not CCY_RE.match(ccy):
        raise ValueError(f"bad currency code {ccy!r}")
    if offline:
        raise NetworkDisabled("offline mode: NBP not called")
    import requests
    s = session or requests.Session()
    day = on
    for _ in range(max_back):
        url = NBP_URL.format(ccy=ccy, date=day.isoformat())
        assert_allowed(url)
        r = s.get(url, timeout=10, headers={"Accept": "application/json"})
        if r.status_code == 404:  # no table published that day
            day -= dt.timedelta(days=1)
            continue
        r.raise_for_status()
        rates = r.json().get("rates") or []
        if rates:
            return float(rates[0]["mid"]), dt.date.fromisoformat(rates[0]["effectiveDate"])
        day -= dt.timedelta(days=1)
    return None


def fx_fill(pnl: pl.DataFrame, offline: bool, fetch=nbp_rate) -> tuple[pl.DataFrame, pl.DataFrame]:
    """For foreign-currency rows lacking a rate, ask NBP for the 15th of the month (or the
    sheet's fx_date). Returns (fx_rates frame, notes). Never raises on network failure."""
    need = (pnl.filter((pl.col("currency") != "PLN") & pl.col("fx_rate").is_null())
            .select(["currency", "year", "month", "fx_date"]).unique())
    rows, notes = [], []
    for r in need.iter_rows(named=True):
        on = r["fx_date"] or dt.date(r["year"], r["month"], 15)
        try:
            got = fetch(r["currency"], on, offline=offline)
        except NetworkDisabled as e:
            notes.append(str(e))
            break
        except Exception as e:  # network error: degrade, do not fail the app
            notes.append(f"NBP {r['currency']} {on}: {type(e).__name__}: {e}")
            continue
        if got:
            rows.append({"ccy": r["currency"], "date": on, "rate": got[0], "source": f"nbp:{got[1]}"})
    schema = {"ccy": pl.Utf8, "date": pl.Date, "rate": pl.Float64, "source": pl.Utf8}
    fx = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
    return fx, pl.DataFrame({"note": notes}, schema={"note": pl.Utf8})


def quotes(tickers: list[str], offline: bool = False) -> pl.DataFrame:
    """Latest prices from Yahoo via yfinance. Only the ticker strings are sent."""
    schema = {"ticker": pl.Utf8, "ts": pl.Datetime("us"), "price": pl.Float64, "currency": pl.Utf8, "source": pl.Utf8}
    clean = sorted({t.strip().upper() for t in tickers if t and TICKER_RE.match(t.strip().upper())})
    if offline:
        raise NetworkDisabled("offline mode: Yahoo not called")
    if not clean:
        return pl.DataFrame(schema=schema)
    import yfinance as yf
    rows = []
    now = dt.datetime.now()
    data = yf.Tickers(" ".join(clean))
    for t in clean:
        try:
            fi = data.tickers[t].fast_info
            price = fi.get("lastPrice") or fi.get("last_price")
            ccy = fi.get("currency")
        except Exception:
            price, ccy = None, None
        if price is not None:
            rows.append({"ticker": t, "ts": now, "price": float(price), "currency": ccy, "source": "yahoo"})
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
