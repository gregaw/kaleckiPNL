"""PnL engines and the DuckDB schema. Pure functions on polars frames: no I/O, no network.

Rental: reproduces the sheet's own arithmetic, converts foreign units to the base currency,
and rolls up per unit, per year, and per month for tax. Trades: FIFO/LIFO/average cost basis."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import duckdb
import polars as pl

from kalecki.config import AnalysisConfig, TaxConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
  unit_key TEXT PRIMARY KEY, label TEXT, kind TEXT, city TEXT, address TEXT, unit_no TEXT,
  owner TEXT, manager TEXT, value_t0 DOUBLE, area_m2 DOUBLE, floor DOUBLE, kw TEXT,
  acquired_on DATE, currency TEXT, in_reference BOOLEAN, in_ledger BOOLEAN);
CREATE TABLE IF NOT EXISTS rent_ledger (
  unit_key TEXT, year INTEGER, month INTEGER, manager TEXT, city TEXT, address TEXT,
  unit_no TEXT, owner TEXT, contract_rent DOUBLE, media_advance DOUBLE, tenant_payment DOUBLE,
  mgmt_invoice DOUBLE, hoa_fee DOUBLE, electricity DOUBLE, repairs DOUBLE, transfer DOUBLE,
  expected_transfer_sheet DOUBLE, balance_sheet DOUBLE, non_tax_costs DOUBLE,
  taxable_profit_sheet DOUBLE, fx_rate DOUBLE, fx_date DATE, row_no INTEGER);
CREATE TABLE IF NOT EXISTS trades (
  trade_id INTEGER, ts TIMESTAMP, ticker TEXT, side TEXT, qty DOUBLE, price DOUBLE,
  fee DOUBLE, currency TEXT, account TEXT);
CREATE TABLE IF NOT EXISTS fx_rates (ccy TEXT, date DATE, rate DOUBLE, source TEXT);
CREATE TABLE IF NOT EXISTS quotes (ticker TEXT, ts TIMESTAMP, price DOUBLE, currency TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS sync_log (ts TIMESTAMP, source TEXT, rows INTEGER, checksum TEXT, note TEXT);
"""
TABLES = ["units", "rent_ledger", "trades", "fx_rates", "quotes", "sync_log"]


def open_db(tables: dict[str, pl.DataFrame] | None = None) -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB with the schema applied and the given frames loaded."""
    con = duckdb.connect()
    con.execute(SCHEMA)
    for name, df in (tables or {}).items():
        if name not in TABLES or df.height == 0:
            continue
        con.register("_src", df)
        cols = ", ".join(f'"{c}"' for c in df.columns)
        con.execute(f'INSERT INTO {name} ({cols}) SELECT {cols} FROM _src')
        con.unregister("_src")
    return con


# ----------------------------------------------------------------------------- rental

def _z(c: str) -> pl.Expr:
    return pl.col(c).fill_null(0.0)


def monthly_pnl(ledger: pl.DataFrame, units: pl.DataFrame, analysis: AnalysisConfig | None = None) -> pl.DataFrame:
    """One row per ledger row with the sheet's formulas recomputed, the media settlement,
    net income, and base-currency (`_pln`) versions of the money columns.

    expected_transfer = contract_rent − mgmt_invoice − repairs − non_tax_costs
    balance           = transfer − expected_transfer          (+ overpaid, − short)
    taxable_profit    = transfer + repairs + mgmt_invoice     (ryczałt base, gross rent net of media)
    media_result      = media_advance − hoa_fee − electricity (only when the owner pays those)
    net_income        = taxable_profit − non_tax_costs + media_result
    """
    a = analysis or AnalysisConfig()
    df = ledger.join(units.select(["unit_key", "kind", "currency", "value_t0", "area_m2"]), on="unit_key", how="left")
    df = df.with_columns([
        pl.col("currency").fill_null("PLN"),
        pl.col("kind").fill_null("mieszkanie"),
        (_z("contract_rent") - _z("mgmt_invoice") - _z("repairs") - _z("non_tax_costs")).round(2).alias("expected_transfer"),
        (_z("transfer") + _z("repairs") + _z("mgmt_invoice")).round(2).alias("taxable_profit"),
    ])
    df = df.with_columns((_z("transfer") - pl.col("expected_transfer")).round(2).alias("balance"))
    media = pl.lit(0.0)
    if a.owner_pays_hoa:
        media = media - _z("hoa_fee")
    if a.owner_pays_electricity:
        media = media - _z("electricity")
    if a.owner_pays_hoa or a.owner_pays_electricity:
        media = media + _z("media_advance")
    df = df.with_columns(media.round(2).alias("media_result"))
    df = df.with_columns((pl.col("taxable_profit") - _z("non_tax_costs") + pl.col("media_result")).round(2).alias("net_income"))
    # FX: 1 for base-currency units; otherwise the row's rate, carried forward/back within the unit.
    df = df.sort(["unit_key", "year", "month"]).with_columns(
        pl.when(pl.col("currency") == "PLN").then(pl.lit(1.0))
        .otherwise(pl.col("fx_rate").forward_fill().backward_fill().over("unit_key")).alias("fx")
    )
    for c in ["contract_rent", "transfer", "expected_transfer", "balance", "taxable_profit", "net_income", "repairs", "mgmt_invoice", "non_tax_costs"]:
        df = df.with_columns((_z(c) * pl.col("fx")).round(2).alias(f"{c}_pln"))
    df = df.with_columns(pl.date(pl.col("year"), pl.col("month"), 1).alias("period"))
    return df


def discrepancies(pnl: pl.DataFrame, tol: float = 0.01) -> pl.DataFrame:
    """Rows where the sheet's stored formula results differ from the recomputed ones."""
    checks = [("expected_transfer_sheet", "expected_transfer"), ("balance_sheet", "balance"), ("taxable_profit_sheet", "taxable_profit")]
    parts = []
    for sheet_col, ours in checks:
        p = pnl.filter(pl.col(sheet_col).is_not_null() & ((pl.col(sheet_col) - pl.col(ours)).abs() > tol)).select([
            "unit_key", "year", "month", "row_no", pl.lit(ours).alias("column"),
            pl.col(sheet_col).alias("sheet_value"), pl.col(ours).alias("computed"),
        ])
        parts.append(p)
    return pl.concat(parts).sort(["unit_key", "year", "month"]) if parts else pl.DataFrame()


def arrears(pnl: pl.DataFrame) -> pl.DataFrame:
    """Per unit: cumulative balance over time, current outstanding, and how many months at the
    end have been short. Negative balance = tenant/manager owes."""
    ts = pnl.sort(["unit_key", "year", "month"]).with_columns(
        pl.col("balance_pln").cum_sum().over("unit_key").round(2).alias("cum_balance_pln"))
    def trailing_short(s: pl.Series) -> int:
        n = 0
        for v in reversed(s.to_list()):
            if v is not None and v < -0.005:
                n += 1
            else:
                break
        return n
    cur = ts.group_by("unit_key").agg([
        pl.col("cum_balance_pln").last().alias("outstanding_pln"),
        pl.col("balance_pln").map_batches(lambda s: pl.Series([trailing_short(s)]), return_dtype=pl.Int64).first().alias("months_short"),
        pl.col("period").last().alias("as_of"),
    ]).sort("outstanding_pln")
    return cur


def arrears_series(pnl: pl.DataFrame) -> pl.DataFrame:
    return pnl.sort(["unit_key", "year", "month"]).with_columns(
        pl.col("balance_pln").cum_sum().over("unit_key").round(2).alias("cum_balance_pln")
    ).select(["unit_key", "period", "year", "month", "balance_pln", "cum_balance_pln"])


def unit_year_summary(pnl: pl.DataFrame, units: pl.DataFrame) -> pl.DataFrame:
    g = pnl.group_by(["unit_key", "year"]).agg([
        pl.len().alias("months"),
        (pl.col("contract_rent").fill_null(0) > 0).sum().alias("occupied_months"),
        pl.col("contract_rent_pln").sum().round(2).alias("contract_rent_pln"),
        pl.col("transfer_pln").sum().round(2).alias("transfer_pln"),
        pl.col("expected_transfer_pln").sum().round(2).alias("expected_transfer_pln"),
        pl.col("balance_pln").sum().round(2).alias("balance_pln"),
        pl.col("mgmt_invoice_pln").sum().round(2).alias("mgmt_pln"),
        pl.col("repairs_pln").sum().round(2).alias("repairs_pln"),
        pl.col("non_tax_costs_pln").sum().round(2).alias("non_tax_costs_pln"),
        (_z("hoa_fee") * pl.col("fx")).sum().round(2).alias("hoa_pln"),
        (_z("electricity") * pl.col("fx")).sum().round(2).alias("electricity_pln"),
        (pl.col("media_result") * pl.col("fx")).sum().round(2).alias("media_result_pln"),
        pl.col("taxable_profit_pln").sum().round(2).alias("taxable_profit_pln"),
        pl.col("net_income_pln").sum().round(2).alias("net_income_pln"),
    ])
    g = g.join(units.select(["unit_key", "kind", "city", "value_t0", "area_m2", "currency"]), on="unit_key", how="left")
    g = g.with_columns(
        pl.when(pl.col("value_t0") > 0).then(pl.col("net_income_pln") / pl.col("value_t0")).otherwise(None).alias("yield_on_t0"),
        (pl.col("months") < 12).alias("partial_year"),
    )
    return g.sort(["unit_key", "year"])


def ryczalt(base: float, tax: TaxConfig) -> float:
    if base <= 0:
        return 0.0
    low = min(base, tax.threshold_pln)
    high = max(base - tax.threshold_pln, 0.0)
    return round(low * tax.rate_low + high * tax.rate_high, 2)


def tax_estimate(pnl: pl.DataFrame, tax: TaxConfig) -> pl.DataFrame:
    """Per year: the ryczałt base across all units and the tax on it."""
    y = pnl.group_by("year").agg(pl.col("taxable_profit_pln").sum().round(2).alias("base_pln")).sort("year")
    return y.with_columns([
        pl.col("base_pln").map_elements(lambda b: ryczalt(b, tax), return_dtype=pl.Float64).alias("tax_pln"),
        (pl.col("base_pln") > tax.threshold_pln).alias("above_threshold"),
    ]).with_columns(
        pl.when(pl.col("base_pln") > 0).then(pl.col("tax_pln") / pl.col("base_pln")).otherwise(0.0).alias("effective_rate"))


def tax_schedule(pnl: pl.DataFrame, tax: TaxConfig) -> pl.DataFrame:
    """Per month: base, cumulative base within the year, the advance due, its due date, and
    the band in force. The advance is the increase in tax on the cumulative base."""
    m = pnl.group_by(["year", "month"]).agg(pl.col("taxable_profit_pln").sum().round(2).alias("base_pln")).sort(["year", "month"])
    m = m.with_columns(pl.col("base_pln").cum_sum().over("year").round(2).alias("cum_base_pln"))
    m = m.with_columns(pl.col("cum_base_pln").map_elements(lambda b: ryczalt(b, tax), return_dtype=pl.Float64).alias("cum_tax_pln"))
    m = m.with_columns((pl.col("cum_tax_pln") - pl.col("cum_tax_pln").shift(1, fill_value=0.0).over("year")).round(2).alias("advance_pln"))
    def due(y, mo):
        y2, m2 = (y + 1, 1) if mo == 12 else (y, mo + 1)
        return dt.date(y2, m2, min(tax.advance_due_day, 28))
    m = m.with_columns([
        pl.struct(["year", "month"]).map_elements(lambda s: due(s["year"], s["month"]), return_dtype=pl.Date).alias("due_on"),
        pl.when(pl.col("cum_base_pln") > tax.threshold_pln).then(pl.lit(f"{tax.rate_high:.1%}")).otherwise(pl.lit(f"{tax.rate_low:.1%}")).alias("band"),
    ])
    return m


def portfolio_timeseries(pnl: pl.DataFrame) -> pl.DataFrame:
    return pnl.group_by("period").agg([
        pl.col("transfer_pln").sum().round(2).alias("transfer_pln"),
        pl.col("expected_transfer_pln").sum().round(2).alias("expected_transfer_pln"),
        pl.col("taxable_profit_pln").sum().round(2).alias("taxable_profit_pln"),
        pl.col("net_income_pln").sum().round(2).alias("net_income_pln"),
        pl.col("balance_pln").sum().round(2).alias("balance_pln"),
        pl.len().alias("units"),
    ]).sort("period")


def expected_cash(pnl: pl.DataFrame) -> pl.DataFrame:
    """What each manager should transfer next month, from the latest month's contract rents."""
    if pnl.height == 0:
        return pl.DataFrame(schema={"manager": pl.Utf8, "units": pl.Int64, "expected_pln": pl.Float64})
    last = pnl.select(pl.col("period").max()).item()
    cur = pnl.filter(pl.col("period") == last)
    return cur.group_by(pl.col("manager").fill_null("-")).agg([
        pl.len().alias("units"),
        ((_z("contract_rent") - _z("mgmt_invoice")) * pl.col("fx")).sum().round(2).alias("expected_pln"),
    ]).sort("expected_pln", descending=True)


# ----------------------------------------------------------------------------- trades

@dataclass
class Lot:
    ts: dt.datetime
    qty: float
    unit_cost: float


REALIZED_SCHEMA = {"trade_id": pl.Int32, "ts": pl.Datetime("us"), "ticker": pl.Utf8, "qty": pl.Float64,
                   "proceeds": pl.Float64, "cost": pl.Float64, "fee": pl.Float64, "realized_pnl": pl.Float64, "currency": pl.Utf8}
POSITION_SCHEMA = {"ticker": pl.Utf8, "qty": pl.Float64, "cost_basis": pl.Float64, "avg_cost": pl.Float64, "currency": pl.Utf8}
LOT_SCHEMA = {"ticker": pl.Utf8, "opened": pl.Datetime("us"), "qty": pl.Float64, "unit_cost": pl.Float64}


def cost_basis(trades: pl.DataFrame, method: str = "fifo", fees: str = "capitalize") -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Return (realized, positions, open_lots). method: fifo | lifo | average.
    fees: capitalize (buy fee into cost, sell fee off proceeds) or expense (every fee is its own
    realized loss at trade time; cost basis and proceeds exclude fees)."""
    if method not in ("fifo", "lifo", "average") or fees not in ("capitalize", "expense"):
        raise ValueError("method must be fifo|lifo|average, fees capitalize|expense")
    lots: dict[str, list[Lot]] = {}
    ccy: dict[str, str] = {}
    realized = []
    for t in trades.sort(["ts", "trade_id"]).iter_rows(named=True):
        tk, side, qty, price, fee = t["ticker"], t["side"].upper(), float(t["qty"]), float(t["price"]), float(t["fee"] or 0.0)
        ccy.setdefault(tk, t["currency"] or "PLN")
        book = lots.setdefault(tk, [])
        if side == "BUY":
            cost = qty * price + (fee if fees == "capitalize" else 0.0)
            if method == "average" and book:
                tot_q = sum(l.qty for l in book) + qty
                tot_c = sum(l.qty * l.unit_cost for l in book) + cost
                book[:] = [Lot(book[0].ts, tot_q, tot_c / tot_q)]
            else:
                book.append(Lot(t["ts"], qty, cost / qty))
            if fees == "expense" and fee:
                realized.append((t["trade_id"], t["ts"], tk, 0.0, 0.0, 0.0, fee, -fee, ccy[tk]))
            continue
        # SELL
        remaining = qty
        matched_cost = 0.0
        order = list(reversed(book)) if method == "lifo" else book
        for lot in list(order):
            if remaining <= 1e-12:
                break
            take = min(lot.qty, remaining)
            matched_cost += take * lot.unit_cost
            lot.qty -= take
            remaining -= take
            if lot.qty <= 1e-12:
                book.remove(lot)
        if remaining > 1e-9:
            raise ValueError(f"{tk}: selling {qty} on {t['ts']} exceeds holdings by {remaining}")
        proceeds = qty * price - (fee if fees == "capitalize" else 0.0)
        pnl = proceeds - matched_cost - (fee if fees == "expense" else 0.0)
        realized.append((t["trade_id"], t["ts"], tk, qty, round(proceeds, 2), round(matched_cost, 2), fee, round(pnl, 2), ccy[tk]))
    pos = []
    open_lots = []
    for tk, book in lots.items():
        q = sum(l.qty for l in book)
        c = sum(l.qty * l.unit_cost for l in book)
        if q > 1e-12:
            pos.append((tk, round(q, 6), round(c, 2), round(c / q, 4), ccy[tk]))
        for l in book:
            if l.qty > 1e-12:
                open_lots.append((tk, l.ts, round(l.qty, 6), round(l.unit_cost, 4)))
    realized_df = pl.DataFrame(realized, schema=REALIZED_SCHEMA, orient="row") if realized else pl.DataFrame(schema=REALIZED_SCHEMA)
    pos_df = pl.DataFrame(pos, schema=POSITION_SCHEMA, orient="row") if pos else pl.DataFrame(schema=POSITION_SCHEMA)
    lots_df = pl.DataFrame(open_lots, schema=LOT_SCHEMA, orient="row") if open_lots else pl.DataFrame(schema=LOT_SCHEMA)
    return realized_df, pos_df.sort("ticker"), lots_df.sort(["ticker", "opened"])


def unrealized_pnl(positions: pl.DataFrame, quotes: pl.DataFrame) -> pl.DataFrame:
    """quotes: ticker, price (latest). Positions with no quote keep null market value."""
    q = quotes.sort("ts").group_by("ticker").agg(pl.col("price").last()) if "ts" in quotes.columns else quotes.select(["ticker", "price"])
    out = positions.join(q, on="ticker", how="left")
    return out.with_columns([
        (pl.col("qty") * pl.col("price")).round(2).alias("market_value"),
    ]).with_columns((pl.col("market_value") - pl.col("cost_basis")).round(2).alias("unrealized_pnl"))
