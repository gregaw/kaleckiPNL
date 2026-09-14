"""Returns from pasted valuations, the problems report, and the helpers the UI shows.
Pure polars; every threshold comes from AnalysisConfig."""
from __future__ import annotations

import re

import polars as pl

from kalecki.config import AnalysisConfig, Config, TaxConfig

PRICE_SCHEMA = {"year": pl.Int32, "city": pl.Utf8, "price_m2": pl.Float64, "garage_price": pl.Float64}
GARAGE_NAMES = {"garaz", "garage", "garaze", "parking", "miejsce postojowe"}


def _num(s: str) -> float | None:
    s = s.strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    s = re.sub(r"[^\d.\-]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def _split(line: str) -> list[str]:
    if "\t" in line:
        return line.split("\t")
    if ";" in line:
        return line.split(";")
    return re.split(r"\s{2,}|\s+", line.strip())


def parse_price_table(text: str) -> pl.DataFrame:
    """Cells pasted from a sheet. Shapes:
        year  price          -> one price for every city (city = null)
        year  Krakow  Warszawa  garaz   (header row names the columns; 'garaz' is a garage price)
    Returns rows (year, city|null, price_m2, garage_price|null)."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    if not lines:
        return pl.DataFrame(schema=PRICE_SCHEMA)
    header: list[str] | None = None
    first = _split(lines[0])
    if _num(first[0]) is None or not (1900 < (_num(first[0]) or 0) < 2200):
        header = [c.strip() for c in first]
        lines = lines[1:]
    rows = []
    for l in lines:
        cells = _split(l)
        y = _num(cells[0])
        if y is None or not (1900 < y < 2200):
            raise ValueError(f"valuations: first column must be a year, got {cells[0]!r}")
        vals = cells[1:]
        if header is None:
            if len(vals) != 1:
                raise ValueError("valuations: several columns need a header row naming the cities")
            rows.append((int(y), None, _num(vals[0]), None))
            continue
        names = header[1:len(vals) + 1]
        garage = None
        for name, v in zip(names, vals):
            n = name.strip().lower()
            if n in GARAGE_NAMES:
                garage = _num(v)
        for name, v in zip(names, vals):
            n = name.strip().lower()
            if n in GARAGE_NAMES:
                continue
            price = _num(v)
            if price is None:
                continue
            rows.append((int(y), None if n in {"", "cena", "price", "pln/m2", "all", "*"} else name.strip(), price, garage))
        if garage is not None and not any(r[0] == int(y) and r[1] is None for r in rows) and len(names) == 1:
            rows.append((int(y), None, None, garage))
    df = pl.DataFrame(rows, schema=PRICE_SCHEMA, orient="row") if rows else pl.DataFrame(schema=PRICE_SCHEMA)
    return df.sort(["year", "city"])


def unit_values(units: pl.DataFrame, prices: pl.DataFrame, garage_value_index: bool = True) -> pl.DataFrame:
    """Value of each unit in each year the price table covers.
    flat:   price_m2(year, city or default) × area_m2
    garage: garage_price(year) if given, else value_t0 × price(year) / price(first year)."""
    schema = {"unit_key": pl.Utf8, "year": pl.Int32, "value_pln": pl.Float64, "value_source": pl.Utf8}
    if prices.height == 0 or units.height == 0:
        return pl.DataFrame(schema=schema)
    years = prices.select("year").unique().sort("year")
    grid = units.select(["unit_key", pl.col("kind").cast(pl.Utf8), pl.col("city").cast(pl.Utf8), pl.col("area_m2").cast(pl.Float64), pl.col("value_t0").cast(pl.Float64)]).join(years, how="cross")
    city_px = prices.filter(pl.col("city").is_not_null()).select(["year", "city", pl.col("price_m2").alias("px_city")])
    def_px = (prices.filter(pl.col("city").is_null()).group_by("year")
              .agg([pl.col("price_m2").drop_nulls().first().alias("px_default"), pl.col("garage_price").drop_nulls().first().alias("garage_px")]))
    # a per-city garage price, if the sheet had one on a city row
    grid = grid.join(city_px, on=["year", "city"], how="left").join(def_px, on="year", how="left")
    grid = grid.with_columns(pl.coalesce([pl.col("px_city"), pl.col("px_default")]).alias("px"))
    first_year = years.select(pl.col("year").min()).item()
    base_px = grid.filter(pl.col("year") == first_year).select(["unit_key", pl.col("px").alias("px0")])
    grid = grid.join(base_px, on="unit_key", how="left")
    is_garage = pl.col("kind").str.starts_with("gara")
    flat_value = pl.col("px") * pl.col("area_m2")
    garage_value = pl.when(pl.col("garage_px").is_not_null()).then(pl.col("garage_px")).otherwise(
        pl.when(pl.lit(garage_value_index) & (pl.col("px0") > 0)).then(pl.col("value_t0") * pl.col("px") / pl.col("px0")).otherwise(pl.col("value_t0")))
    grid = grid.with_columns([
        pl.when(is_garage).then(garage_value).otherwise(flat_value).round(0).alias("value_pln"),
        pl.when(is_garage & pl.col("garage_px").is_not_null()).then(pl.lit("garage price"))
        .when(is_garage).then(pl.lit("T0 × city index" if garage_value_index else "T0"))
        .otherwise(pl.lit("price/m² × area")).alias("value_source"),
    ])
    return grid.select(list(schema)).sort(["unit_key", "year"])


def returns(summary: pl.DataFrame, units: pl.DataFrame, values: pl.DataFrame, tax_by_year: pl.DataFrame, cfg: Config) -> pl.DataFrame:
    """Per unit per year: income, capital and total return, before and after tax.
    income_return  = annualised net income / value at the start of the year (previous year's
                     value, else this year's, else T0 value)
    capital_return = (value − previous value) / previous value   (null in the first year)
    total_return   = income_return + capital_return (capital counted as 0 when unknown)
    Tax is the year's ryczałt allocated to units pro rata to their taxable profit."""
    s = summary.select(["unit_key", "year", "months", "partial_year", "taxable_profit_pln", "net_income_pln"])
    s = s.join(tax_by_year.select(["year", "base_pln", "tax_pln"]), on="year", how="left")
    s = s.with_columns(
        pl.when(pl.col("base_pln") > 0).then(pl.col("tax_pln") * pl.col("taxable_profit_pln") / pl.col("base_pln")).otherwise(0.0).round(2).alias("tax_alloc_pln"))
    s = s.with_columns((pl.col("net_income_pln") - pl.col("tax_alloc_pln")).round(2).alias("net_after_tax_pln"))
    ann = pl.when(pl.col("months") > 0).then(12.0 / pl.col("months")).otherwise(1.0)
    s = s.with_columns([(pl.col("net_income_pln") * ann).round(2).alias("net_income_ann_pln"),
                        (pl.col("net_after_tax_pln") * ann).round(2).alias("net_after_tax_ann_pln")])
    v = values.select(["unit_key", "year", "value_pln"])
    prev = values.select(["unit_key", (pl.col("year") + 1).alias("year"), pl.col("value_pln").alias("value_prev_pln")])
    s = s.join(v, on=["unit_key", "year"], how="left").join(prev, on=["unit_key", "year"], how="left")
    s = s.join(units.select(["unit_key", "kind", "city", "value_t0"]), on="unit_key", how="left")
    s = s.with_columns(pl.coalesce([pl.col("value_prev_pln"), pl.col("value_pln"), pl.col("value_t0")]).alias("value_base_pln"))
    s = s.with_columns([
        pl.when(pl.col("value_base_pln") > 0).then(pl.col("net_income_ann_pln") / pl.col("value_base_pln")).alias("income_return"),
        pl.when(pl.col("value_base_pln") > 0).then(pl.col("net_after_tax_ann_pln") / pl.col("value_base_pln")).alias("income_return_after_tax"),
        pl.when(pl.col("value_prev_pln") > 0).then((pl.col("value_pln") - pl.col("value_prev_pln")) / pl.col("value_prev_pln")).alias("capital_return"),
    ])
    s = s.with_columns([
        (pl.col("income_return") + pl.col("capital_return").fill_null(0.0)).alias("total_return"),
        (pl.col("income_return_after_tax") + pl.col("capital_return").fill_null(0.0)).alias("total_return_after_tax"),
    ])
    return s.sort(["unit_key", "year"])


def portfolio_returns(ret: pl.DataFrame) -> pl.DataFrame:
    """One row per year: sums and value-weighted returns."""
    g = ret.group_by("year").agg([
        pl.len().alias("units"),
        pl.col("net_income_pln").sum().round(2).alias("net_income_pln"),
        pl.col("net_after_tax_pln").sum().round(2).alias("net_after_tax_pln"),
        pl.col("net_income_ann_pln").sum().round(2).alias("net_income_ann_pln"),
        pl.col("net_after_tax_ann_pln").sum().round(2).alias("net_after_tax_ann_pln"),
        pl.col("value_pln").sum().alias("value_pln"),
        pl.col("value_base_pln").filter(pl.col("income_return").is_not_null()).sum().alias("value_base_pln"),
        pl.col("value_pln").filter(pl.col("capital_return").is_not_null()).sum().alias("v_now"),
        pl.col("value_prev_pln").filter(pl.col("capital_return").is_not_null()).sum().alias("v_prev"),
        pl.col("partial_year").any().alias("partial_year"),
    ]).sort("year")
    g = g.with_columns([
        pl.when(pl.col("value_base_pln") > 0).then(pl.col("net_income_ann_pln") / pl.col("value_base_pln")).alias("income_return"),
        pl.when(pl.col("value_base_pln") > 0).then(pl.col("net_after_tax_ann_pln") / pl.col("value_base_pln")).alias("income_return_after_tax"),
        pl.when(pl.col("v_prev") > 0).then((pl.col("v_now") - pl.col("v_prev")) / pl.col("v_prev")).alias("capital_return"),
    ])
    return g.with_columns([
        (pl.col("income_return") + pl.col("capital_return").fill_null(0.0)).alias("total_return"),
        (pl.col("income_return_after_tax") + pl.col("capital_return").fill_null(0.0)).alias("total_return_after_tax"),
    ]).drop(["v_now", "v_prev"])


def returns_matrix(ret: pl.DataFrame, total: pl.DataFrame, metric: str) -> pl.DataFrame:
    """unit × year wide table of one metric, with a TOTAL row, ready to paste back into a sheet."""
    if ret.height == 0:
        return pl.DataFrame()
    wide = ret.select(["unit_key", "year", metric]).pivot(on="year", index="unit_key", values=metric)
    years = sorted(int(c) for c in wide.columns if c != "unit_key")
    wide = wide.select(["unit_key"] + [str(y) for y in years])
    if metric in total.columns and total.height:
        tot = total.select(["year", metric]).with_columns(pl.lit("TOTAL").alias("unit_key")).pivot(on="year", index="unit_key", values=metric)
        tot = tot.select(["unit_key"] + [pl.col(str(y)) if str(y) in tot.columns else pl.lit(None, dtype=pl.Float64).alias(str(y)) for y in years])
        wide = pl.concat([wide, tot.cast(wide.schema)])
    return wide


PROBLEM_SCHEMA = {"unit_key": pl.Utf8, "year": pl.Int32, "month": pl.Int32, "kind": pl.Utf8, "severity": pl.Utf8, "detail": pl.Utf8}
SEVERITY = {"arrears": "high", "duplicate_row": "high", "fx_missing": "high",
            "underpaid": "medium", "missing_month": "medium", "not_in_reference": "medium",
            "formula_mismatch": "medium", "no_valuation_data": "medium",
            "vacancy": "low", "not_in_ledger": "low", "media_shortfall": "low", "rent_unchanged": "low"}


def problems(pnl: pl.DataFrame, units: pl.DataFrame, arrears_df: pl.DataFrame, discrepancies_df: pl.DataFrame,
             cfg: AnalysisConfig | None = None) -> pl.DataFrame:
    """Every finding as one row (unit, year, month, kind, severity, detail)."""
    a = cfg or AnalysisConfig()
    found: list[tuple] = []

    def add(unit, year, month, kind, detail):
        found.append((unit, year, month, kind, SEVERITY[kind], detail))

    for r in arrears_df.iter_rows(named=True):
        if r["outstanding_pln"] is not None and (r["outstanding_pln"] < -a.arrears_threshold or r["months_short"] >= a.arrears_months):
            add(r["unit_key"], r["as_of"].year, r["as_of"].month, "arrears",
                f"outstanding {r['outstanding_pln']:.2f} PLN, short for {r['months_short']} month(s)")
    for r in pnl.filter(pl.col("balance") < -0.005).iter_rows(named=True):
        add(r["unit_key"], r["year"], r["month"], "underpaid",
            f"transfer {r['transfer'] or 0:.2f} vs expected {r['expected_transfer']:.2f} ({r['currency']}); manager {r['manager'] or '-'}")
    for r in pnl.filter(pl.col("contract_rent").fill_null(0) <= 0).iter_rows(named=True):
        add(r["unit_key"], r["year"], r["month"], "vacancy", "no contract rent this month")
    # gaps and duplicates
    for u, grp in pnl.group_by("unit_key", maintain_order=True):
        key = u[0]
        periods = sorted(set((y, m) for y, m in grp.select(["year", "month"]).iter_rows()))
        if len(periods) != grp.height:
            dups = grp.group_by(["year", "month"]).len().filter(pl.col("len") > 1)
            for y, m, n in dups.iter_rows():
                add(key, y, m, "duplicate_row", f"{n} rows for the same month")
        if periods:
            y, m = periods[0]
            last = periods[-1]
            while (y, m) < last:
                m += 1
                if m > 12:
                    y, m = y + 1, 1
                if (y, m) not in set(periods) and (y, m) < last:
                    add(key, y, m, "missing_month", "no ledger row for this month")
    for r in units.filter(pl.col("in_ledger") & ~pl.col("in_reference")).iter_rows(named=True):
        add(r["unit_key"], None, None, "not_in_reference", "unit appears in the ledger but not in Reference (no area, value, T0)")
    for r in units.filter(pl.col("in_reference") & ~pl.col("in_ledger")).iter_rows(named=True):
        add(r["unit_key"], None, None, "not_in_ledger", "unit listed in Reference but has no ledger rows")
    for r in discrepancies_df.iter_rows(named=True):
        add(r["unit_key"], r["year"], r["month"], "formula_mismatch",
            f"{r['column']}: sheet {r['sheet_value']:.2f} vs computed {r['computed']:.2f} (row {r['row_no']})")
    ms = pnl.filter((pl.col("contract_rent").fill_null(0) > 0) & pl.col("media_advance").is_not_null()
                    & ((pl.col("hoa_fee").fill_null(0) + pl.col("electricity").fill_null(0)) > pl.col("media_advance") + 0.005))
    for r in ms.iter_rows(named=True):
        add(r["unit_key"], r["year"], r["month"], "media_shortfall",
            f"media advance {r['media_advance']:.2f} < HOA {r['hoa_fee'] or 0:.2f} + electricity {r['electricity'] or 0:.2f}")
    n = a.rent_unchanged_months
    for u, grp in pnl.sort(["year", "month"]).group_by("unit_key", maintain_order=True):
        rents = grp.select("contract_rent").to_series().to_list()
        if len(rents) > n and rents[-1] and all(x == rents[-1] for x in rents[-n - 1:]):
            last = grp.row(-1, named=True)
            add(u[0], last["year"], last["month"], "rent_unchanged", f"contract rent {rents[-1]:.2f} unchanged for {n}+ months")
    for r in pnl.filter((pl.col("currency") != "PLN") & pl.col("fx").is_null()).iter_rows(named=True):
        add(r["unit_key"], r["year"], r["month"], "fx_missing", f"{r['currency']} unit without an NBP rate; PLN figures are missing")
    rented = pnl.filter(pl.col("contract_rent").fill_null(0) > 0).select("unit_key").unique()
    for r in units.join(rented, on="unit_key", how="inner").filter(
            (~pl.col("kind").str.starts_with("gara") & pl.col("area_m2").is_null()) | pl.col("value_t0").is_null()).iter_rows(named=True):
        add(r["unit_key"], None, None, "no_valuation_data", "missing area or T0 value; return cannot be computed")
    df = pl.DataFrame(found, schema=PROBLEM_SCHEMA, orient="row") if found else pl.DataFrame(schema=PROBLEM_SCHEMA)
    if a.muted:
        df = df.filter(~pl.col("kind").is_in(a.muted))
    order = {"high": 0, "medium": 1, "low": 2}
    return df.with_columns(pl.col("severity").replace_strict(order, return_dtype=pl.Int32).alias("_o")).sort(["_o", "unit_key", "year", "month"]).drop("_o")


# ----------------------------------------------------------------------------- growth-model valuations

VALUE_SCHEMA = {"unit_key": pl.Utf8, "year": pl.Int32, "value_pln": pl.Float64, "value_source": pl.Utf8}


def growth_values(units: pl.DataFrame, growth: dict[str, float], years: list[int], default_growth: float = 0.0) -> pl.DataFrame:
    """Simplified valuation: each unit's T0 value compounded at its city's annual % change.
        value(year) = value_t0 × (1 + g_city) ** (year − T0 year)
    T0 year is the unit's `acquired_on` year; a unit without one is valued from the first year
    asked for. `growth` maps city → annual rate (0.05 = +5 %); cities not listed use
    `default_growth`. Years before T0 are not valued. Same shape as `unit_values`."""
    if units.height == 0 or not years:
        return pl.DataFrame(schema=VALUE_SCHEMA)
    yrs = pl.DataFrame({"year": sorted(set(int(y) for y in years))}, schema={"year": pl.Int32})
    cols = ["unit_key", pl.col("city").cast(pl.Utf8), pl.col("value_t0").cast(pl.Float64)]
    if "acquired_on" in units.columns:
        cols.append(pl.col("acquired_on").dt.year().cast(pl.Int32).alias("t0_year"))
    else:
        cols.append(pl.lit(None, dtype=pl.Int32).alias("t0_year"))
    g = units.select(cols).with_columns(pl.col("t0_year").fill_null(yrs["year"].min()))
    rates = {k.strip().lower(): float(v) for k, v in growth.items()}
    g = g.with_columns(pl.col("city").fill_null("").str.to_lowercase().str.strip_chars()
                       .replace_strict(rates, default=float(default_growth), return_dtype=pl.Float64).alias("rate"))
    grid = g.join(yrs, how="cross").filter(pl.col("year") >= pl.col("t0_year"))
    grid = grid.with_columns([
        (pl.col("value_t0") * (1.0 + pl.col("rate")).pow((pl.col("year") - pl.col("t0_year")).cast(pl.Float64))).round(0).alias("value_pln"),
        pl.concat_str([pl.lit("T0 × (1 "), pl.when(pl.col("rate") >= 0).then(pl.lit("+ ")).otherwise(pl.lit("− ")),
                       (pl.col("rate").abs() * 100).round(2).cast(pl.Utf8), pl.lit(" %)^n")]).alias("value_source"),
    ])
    return grid.select(list(VALUE_SCHEMA)).sort(["unit_key", "year"])


def values_matrix(values: pl.DataFrame, units: pl.DataFrame) -> pl.DataFrame:
    """unit × year wide table of values with city and T0, plus a TOTAL row; what the owner
    eyeballs while typing a growth rate."""
    if values.height == 0:
        return pl.DataFrame()
    wide = values.pivot(on="year", index="unit_key", values="value_pln")
    years = sorted(int(c) for c in wide.columns if c != "unit_key")
    wide = wide.select(["unit_key"] + [str(y) for y in years])
    wide = units.select(["unit_key", "city", "kind", pl.col("value_t0").cast(pl.Float64)]).join(wide, on="unit_key", how="left").sort(["city", "unit_key"])
    tot = wide.select([pl.lit("TOTAL").alias("unit_key"), pl.lit(None, dtype=pl.Utf8).alias("city"), pl.lit(None, dtype=pl.Utf8).alias("kind"),
                       pl.col("value_t0").sum()] + [pl.col(str(y)).sum() for y in years])
    return pl.concat([wide, tot.cast(wide.schema)])


def valuation_gaps(units: pl.DataFrame) -> pl.DataFrame:
    """Units that cannot be valued and why: not in the Reference tab, or in it with an empty
    'wartosc T0' cell. `t0_date` says whether the T0 date parsed; without it the value is
    compounded from the first year shown."""
    cols = ["unit_key", pl.col("in_reference").fill_null(False), pl.col("value_t0").cast(pl.Float64)]
    cols.append(pl.col("acquired_on").is_not_null().alias("t0_date") if "acquired_on" in units.columns else pl.lit(False).alias("t0_date"))
    u = units.select(cols)
    return (u.filter(pl.col("value_t0").is_null() | (pl.col("value_t0") <= 0))
            .with_columns(pl.when(~pl.col("in_reference")).then(pl.lit("not in Reference tab (key differs from the ledger?)"))
                          .otherwise(pl.lit("in Reference but the T0 value cell is empty or not a number")).alias("why"))
            .select(["unit_key", "in_reference", "t0_date", "why"]).sort("unit_key"))


# ----------------------------------------------------------------------------- pivot

PIVOT_DIMS = ["unit_key", "year", "month", "city", "manager", "kind", "currency", "owner"]
PIVOT_MEASURES = ["net_income_pln", "taxable_profit_pln", "contract_rent_pln", "transfer_pln", "expected_transfer_pln", "balance_pln",
                  "repairs_pln", "mgmt_invoice_pln", "non_tax_costs_pln", "media_result", "hoa_fee", "electricity", "media_advance", "value_t0"]
PIVOT_AGGS = {"sum": pl.Expr.sum, "mean": pl.Expr.mean, "min": pl.Expr.min, "max": pl.Expr.max, "count": pl.Expr.count}


def apply_filters(pnl: pl.DataFrame, filters: dict[str, list] | None) -> pl.DataFrame:
    """Keep the rows whose dimension is in the allowed list; an empty list means no filter.
    The pivot and the drill-down behind its cells must see the same rows."""
    df = pnl
    for k, allowed in (filters or {}).items():
        if allowed and k in df.columns:
            df = df.filter(pl.col(k).is_in(allowed))
    return df


def cell_selection(df: pl.DataFrame, picked: tuple | None) -> tuple[dict, str] | None:
    """Turn a clicked (row, column) of an aggregate table into the drill keys and a label.
    A stale click (the table shrank or lost the column since, e.g. after a filter change)
    gives None instead of raising."""
    if not picked or len(picked) != 2:
        return None
    r, c = picked
    c = str(c)
    if not isinstance(r, int) or r < 0 or r >= df.height or c not in df.columns:
        return None
    row = df.row(r, named=True)
    sel = {k: row[k] for k in df.columns if k in PIVOT_DIMS}
    if c.isdigit():
        sel["year"] = int(c)
    v = row[c]
    what = f"{c} = {v:,.2f}" if isinstance(v, float) else f"{c} = {v}"
    return sel, what


def pivot(pnl: pl.DataFrame, rows: list[str], value: str, agg: str = "sum", cols: str | None = None,
          filters: dict[str, list] | None = None) -> pl.DataFrame:
    """Slice the monthly PnL: group by `rows` (and spread `cols` across the columns), aggregate
    `value` with `agg`. `filters` maps a dimension to the allowed values. Returns a long frame
    (rows…, [cols], value) when `cols` is None, else a wide one with a `TOTAL` column.
    `yield_on_t0` is a derived measure: sum(net_income_pln) / T0 value of the units in the group."""
    if agg not in PIVOT_AGGS:
        raise ValueError(f"agg must be one of {list(PIVOT_AGGS)}")
    df = apply_filters(pnl, filters)
    keys = list(dict.fromkeys(rows + ([cols] if cols else [])))
    if not keys:
        raise ValueError("pick at least one row or column dimension")
    if value == "yield_on_t0":
        per_unit = df.group_by(list(dict.fromkeys(keys + ["unit_key"]))).agg([pl.col("net_income_pln").sum().alias("_ni"), pl.col("value_t0").first().alias("_t0")])
        out = per_unit.group_by(keys).agg([pl.col("_ni").sum().alias("_ni"), pl.col("_t0").sum().alias("_t0")])
        out = out.with_columns(pl.when(pl.col("_t0") > 0).then(pl.col("_ni") / pl.col("_t0")).alias(value)).drop(["_ni", "_t0"])
    else:
        out = df.group_by(keys).agg(PIVOT_AGGS[agg](pl.col(value)).alias(value))
        if agg != "count":
            out = out.with_columns(pl.col(value).round(2))
    out = out.sort(keys)
    if not cols:
        return out
    wide = out.pivot(on=cols, index=rows, values=value) if rows else out.pivot(on=cols, index=None, values=value)
    vcols = [c for c in wide.columns if c not in rows]
    wide = wide.select(rows + sorted(vcols, key=lambda c: (len(c), c)))
    if value != "yield_on_t0" and agg in ("sum", "count"):
        wide = wide.with_columns(pl.sum_horizontal([pl.col(c) for c in vcols]).round(2).alias("TOTAL"))
    return wide


def drill(pnl: pl.DataFrame, selected: dict) -> pl.DataFrame:
    """The monthly rows behind an aggregate cell: filter `pnl` on every key in `selected` that is
    dimension of it (unit_key, year, month, city, manager …); measures and other keys are
    ignored, and so is the TOTAL row/column."""
    df = pnl
    for k, v in selected.items():
        if k not in PIVOT_DIMS or k not in df.columns or v is None or v == "TOTAL":
            continue
        if df.schema[k] in (pl.Int32, pl.Int64):
            if isinstance(v, str):
                if not v.lstrip("-").isdigit():
                    continue
                v = int(v)
            elif isinstance(v, float):
                v = int(v)
        df = df.filter(pl.col(k) == v)
    keep = [c for c in ["unit_key", "year", "month", "city", "manager", "currency", "contract_rent", "media_advance", "tenant_payment", "mgmt_invoice",
                        "hoa_fee", "electricity", "repairs", "non_tax_costs", "transfer", "expected_transfer", "balance", "taxable_profit",
                        "media_result", "net_income", "fx", "net_income_pln", "row_no"] if c in df.columns]
    return df.select(keep).sort(["unit_key", "year", "month"])
