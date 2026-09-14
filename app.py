"""kaleckiPNL dashboard. Runs on 127.0.0.1 only; refuses anything else at startup.
Everything shown is computed from the encrypted vault in memory; the only network calls are
the Drive sync (sync.py) and the ticker/currency lookups (kalecki/market.py)."""
from __future__ import annotations

import datetime as dt
import os
import signal

import altair as alt
import polars as pl
import streamlit as st

import engine as e
import sync as sy
from kalecki import analysis as an
from kalecki import market
from kalecki.config import load as load_config
from kalecki.store import KeyStore, Vault

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
STATUS = {"high": "#d03b3b", "medium": "#fab219", "low": "#898781"}
GRID, AXIS, MUTED = "#e1e0d9", "#c3c2b7", "#898781"

st.set_page_config(page_title="kaleckiPNL", page_icon="📒", layout="wide")

address = st.get_option("server.address") or ""
if address not in LOOPBACK:
    st.error(f"Refusing to serve on '{address}'. Start with run.sh or "
             "`streamlit run app.py --server.address 127.0.0.1` — this app is local-only.")
    st.stop()


# ----------------------------------------------------------------------------- state

@st.cache_resource
def _cfg():
    return load_config("config.toml")


cfg = _cfg()
ks = KeyStore(cfg.keyring_service)
vault = Vault(cfg.vault, ks)
st.session_state.setdefault("offline", cfg.offline)
st.session_state.setdefault("prices_text", cfg.valuations_table)
st.session_state.setdefault("notes", [])


def note(msg: str):
    st.session_state["notes"].append(f"{dt.datetime.now():%H:%M:%S} {msg}")


def load_tables() -> dict[str, pl.DataFrame] | None:
    if not vault.exists():
        return None
    try:
        return vault.load()
    except Exception as ex:  # wrong key, tampering
        st.error(f"Vault cannot be opened: {type(ex).__name__}: {ex}")
        return None


def _theme(chart: alt.Chart) -> alt.Chart:
    return (chart.configure_axis(gridColor=GRID, domainColor=AXIS, tickColor=AXIS, labelColor=MUTED, titleColor=MUTED)
            .configure_view(strokeWidth=0).configure_legend(labelColor=MUTED, titleColor=MUTED))


def _cell_selection(ev) -> tuple[int, str] | None:
    try:
        cells = ev.selection.cells
    except AttributeError:
        return None
    return tuple(cells[0]) if cells else None


def table(df: pl.DataFrame, key: str, pct: list[str] | None = None, height=None, drill: pl.DataFrame | None = None):
    """A dataframe with CSV download. With `drill`, clicking a cell shows the monthly rows behind
    it: the selected row's dimensions (unit, year, city, manager…) plus a year taken from a
    year-named column, filtered from `drill` (the monthly PnL)."""
    if df.height == 0:
        st.caption("nothing to show")
        return
    cc = {c: st.column_config.NumberColumn(format="%.1f%%") for c in (pct or []) if c in df.columns}
    for c, t in df.schema.items():
        if c in cc:
            continue
        if t in (pl.Float64, pl.Float32):
            cc[c] = st.column_config.NumberColumn(format="%.2f")
    d = df.with_columns([(pl.col(c) * 100).alias(c) for c in (pct or []) if c in df.columns]) if pct else df
    kw = {"height": height} if height else {}
    if drill is not None:
        ev = st.dataframe(d.to_pandas(), hide_index=True, column_config=cc, width="stretch", key=f"tbl_{key}",
                          on_select="rerun", selection_mode="single-cell", **kw)
        picked = _cell_selection(ev)
        if picked is not None:
            r, c = picked
            row = df.row(r, named=True)
            sel = {k: row[k] for k in df.columns if k in an.PIVOT_DIMS}
            if c.isdigit():
                sel["year"] = int(c)
            what = f"{c} = {row[c]:,.2f}" if isinstance(row[c], float) else f"{c} = {row[c]}"
            show_drill(drill, sel, what, key)
    else:
        st.dataframe(d.to_pandas(), hide_index=True, column_config=cc, width="stretch", **kw)
    st.download_button("Download CSV", df.write_csv(), f"{key}.csv", "text/csv", key=f"dl_{key}")


def show_drill(pnl: pl.DataFrame, sel: dict, what: str, key: str):
    """The monthly rows behind one aggregate, under the table or chart that was clicked."""
    rows = an.drill(pnl, sel)
    dims = ", ".join(f"{k} {v}" for k, v in sel.items() if v is not None and v != "TOTAL" and k in pnl.columns)
    with st.container(border=True):
        st.markdown(f"**Behind {what}** — {dims or 'everything'} · {rows.height} monthly row(s) · click another cell to change")
        if rows.height:
            m = rows.select([pl.col("net_income_pln").sum().alias("net"), pl.col("transfer").sum().alias("tr"), pl.col("balance").sum().alias("bal")]).row(0)
            st.caption(f"net income {m[0]:,.2f} PLN · transfers {m[1]:,.2f} · balance {m[2]:,.2f}")
        table(rows, f"drill_{key}", height=min(420, 38 * rows.height + 40))


# ----------------------------------------------------------------------------- sidebar

with st.sidebar:
    st.title("kaleckiPNL")
    st.caption("local-only · loopback · encrypted at rest")
    st.session_state["offline"] = st.toggle("Offline (no NBP / Yahoo calls)", value=st.session_state["offline"])
    offline = st.session_state["offline"]

    st.subheader("Data")
    src = f"Drive: {cfg.spreadsheet_id[:10]}…" if cfg.spreadsheet else "no spreadsheet configured"
    if cfg.local_file:
        src = f"local file: {cfg.local_file}"
    st.caption(src)
    if st.button("Sync now", type="primary", width="stretch", disabled=not (cfg.spreadsheet or cfg.local_file)):
        with st.spinner("syncing…"):
            try:
                rep = sy.sync(cfg, ks, interactive=True)
                note(str(rep))
                st.success(str(rep))
                st.cache_data.clear()
            except Exception as ex:
                st.error(f"{type(ex).__name__}: {ex}")
    up = st.file_uploader("…or load a local xlsx", type=["xlsx"], help="Same schema as the sheet. Nothing is uploaded anywhere; the file is parsed in memory.")
    if up is not None and st.button("Load file", width="stretch"):
        try:
            rep = sy.ingest(up.getvalue(), f"local:{up.name}", cfg, vault, force=True)
            note(str(rep))
            st.success(str(rep))
        except Exception as ex:
            st.error(f"{type(ex).__name__}: {ex}")

    with st.expander("Google sign-in"):
        st.caption(f"OAuth client: {'stored' if sy.has_oauth_client(ks) else 'missing'} · token: {'stored' if sy.has_token(ks) else 'none'}")
        cj = st.text_area("Paste the Desktop OAuth client JSON once", height=80, placeholder='{"installed": {...}}')
        if st.button("Store in keychain", disabled=not cj.strip()):
            try:
                sy.store_oauth_client(ks, cj)
                st.success("stored; the JSON never touches disk")
            except Exception as ex:
                st.error(str(ex))
        if st.button("Forget Drive token", disabled=not sy.has_token(ks)):
            sy.logout(ks)
            st.info("token removed")

    st.subheader("Market data")
    c1, c2 = st.columns(2)
    do_fx = c1.button("Refresh FX", width="stretch", disabled=offline, help="Asks NBP for a rate per currency and month. Only the currency code and date are sent.")
    do_q = c2.button("Refresh quotes", width="stretch", disabled=offline, help="Asks Yahoo for the tickers in the Trades tab. Only the ticker symbols are sent.")

    st.divider()
    sure = st.checkbox("I want to stop the server")
    if st.button("Shut down", disabled=not sure, width="stretch"):
        st.warning("Stopping. Close this tab.")
        os.kill(os.getpid(), signal.SIGINT)


# ----------------------------------------------------------------------------- data

tables = load_tables()
if tables is None or "rent_ledger" not in tables:
    st.info("No data yet. Use **Sync now** (Drive) or load a local xlsx from the sidebar. "
            "For a dry run: `python -m kalecki.testdata sample.xlsx` and load that.")
    st.stop()

ledger, units = tables["rent_ledger"], tables["units"]
trades = tables.get("trades", pl.DataFrame())
fx_rates = tables.get("fx_rates", pl.DataFrame(schema={"ccy": pl.Utf8, "date": pl.Date, "rate": pl.Float64, "source": pl.Utf8}))
quotes_t = tables.get("quotes", pl.DataFrame(schema={"ticker": pl.Utf8, "ts": pl.Datetime("us"), "price": pl.Float64, "currency": pl.Utf8, "source": pl.Utf8}))


def apply_fx(ledger: pl.DataFrame, units: pl.DataFrame, fx: pl.DataFrame) -> pl.DataFrame:
    """Fill missing fx_rate from fetched NBP rates by currency and month."""
    if fx.height == 0:
        return ledger
    f = fx.with_columns([pl.col("date").dt.year().cast(pl.Int32).alias("year"), pl.col("date").dt.month().cast(pl.Int32).alias("month")])
    f = f.group_by(["ccy", "year", "month"]).agg(pl.col("rate").last().alias("nbp_rate"))
    l = ledger.join(units.select(["unit_key", pl.col("currency").alias("ccy")]), on="unit_key", how="left")
    l = l.join(f, on=["ccy", "year", "month"], how="left")
    return l.with_columns(pl.coalesce([pl.col("fx_rate"), pl.col("nbp_rate")]).alias("fx_rate")).drop(["ccy", "nbp_rate"])


pnl = e.monthly_pnl(apply_fx(ledger, units, fx_rates), units, cfg.analysis)

if do_fx:
    fx_new, notes = market.fx_fill(pnl, offline=offline)
    for n in notes.to_series().to_list():
        note(n)
    if fx_new.height:
        fx_rates = pl.concat([fx_rates, fx_new]).unique(subset=["ccy", "date"], keep="last")
        vault.update(fx_rates=fx_rates)
        note(f"NBP: {fx_new.height} rate(s) fetched")
        pnl = e.monthly_pnl(apply_fx(ledger, units, fx_rates), units, cfg.analysis)
    else:
        note("NBP: nothing to fetch (no foreign rows without a rate)")
if do_q and trades.height:
    try:
        q = market.quotes(trades.select("ticker").unique().to_series().to_list(), offline=offline)
        if q.height:
            quotes_t = pl.concat([quotes_t, q])
            vault.update(quotes=quotes_t)
        note(f"Yahoo: {q.height} quote(s)")
    except Exception as ex:
        note(f"Yahoo: {type(ex).__name__}: {ex}")

summary = e.unit_year_summary(pnl, units)
tax_y = e.tax_estimate(pnl, cfg.tax)
arr = e.arrears(pnl)
disc = e.discrepancies(pnl)
probs = an.problems(pnl, units, arr, disc, cfg.analysis)
ts = e.portfolio_timeseries(pnl)

# ----------------------------------------------------------------------------- header

last_period = pnl.select(pl.col("period").max()).item()
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Units in ledger", int(units.filter(pl.col("in_ledger")).height))
k2.metric("Latest month", f"{last_period:%Y-%m}" if last_period else "—")
k3.metric("Net income, latest month", f"{ts.row(-1, named=True)['net_income_pln']:,.0f} PLN" if ts.height else "—")
k4.metric("Outstanding arrears", f"{arr.filter(pl.col('outstanding_pln') < 0)['outstanding_pln'].sum():,.0f} PLN")
k5.metric("Problems", int(probs.height), help="see the Problems tab")

tab_p, tab_pv, tab_r, tab_u, tab_pr, tab_t, tab_tr, tab_d = st.tabs(["Portfolio", "Pivot", "Returns", "Units", "Problems", "Tax & ops", "Trades", "Data"])

# ----------------------------------------------------------------------------- portfolio
with tab_p:
    st.subheader("Monthly cash across the portfolio")
    long = ts.select(["period", "expected_transfer_pln", "transfer_pln", "net_income_pln"]).unpivot(
        index="period", variable_name="series", value_name="pln").with_columns(
        pl.col("series").replace({"expected_transfer_pln": "expected transfer", "transfer_pln": "actual transfer", "net_income_pln": "net income"}))
    order = ["expected transfer", "actual transfer", "net income"]
    base = alt.Chart(long.to_pandas()).encode(x=alt.X("period:T", title=None, axis=alt.Axis(format="%b %y")))
    hover = alt.selection_point(fields=["period"], nearest=True, on="mouseover", empty=False)
    lines = base.mark_line(strokeWidth=2).encode(
        y=alt.Y("pln:Q", title="PLN"), color=alt.Color("series:N", sort=order, scale=alt.Scale(domain=order, range=[BLUE, ORANGE, AQUA]), legend=alt.Legend(title=None, orient="top")))
    pts = base.mark_point(size=64, filled=True).encode(y="pln:Q", color=alt.Color("series:N", scale=alt.Scale(domain=order, range=[BLUE, ORANGE, AQUA]), legend=None),
                                                      opacity=alt.condition(hover, alt.value(1), alt.value(0)),
                                                      tooltip=[alt.Tooltip("period:T", format="%Y-%m"), "series:N", alt.Tooltip("pln:Q", format=",.0f")]).add_params(hover)
    rule = base.mark_rule(color=AXIS).encode(opacity=alt.condition(hover, alt.value(0.6), alt.value(0)))
    st.altair_chart(_theme((lines + pts + rule).properties(height=300)), width="stretch")

    st.subheader("Yield on T0 value, by unit and year")
    yv = summary.filter(pl.col("yield_on_t0").is_not_null()).select(["unit_key", "year", "yield_on_t0", "months", "net_income_pln"])
    if yv.height:
        yrs = sorted(yv["year"].unique().to_list())
        bars = alt.Chart(yv.to_pandas()).mark_bar(cornerRadiusEnd=4, size=14).encode(
            y=alt.Y("unit_key:N", title=None, sort="-x"), x=alt.X("yield_on_t0:Q", title="net income / T0 value", axis=alt.Axis(format=".0%")),
            color=alt.Color("year:O", scale=alt.Scale(domain=yrs, range=[BLUE, ORANGE, AQUA, "#eda100", "#e87ba4"][:len(yrs)]), legend=alt.Legend(title="year", orient="top")),
            yOffset="year:O",
            tooltip=["unit_key:N", "year:O", alt.Tooltip("yield_on_t0:Q", format=".1%"), "months:Q", alt.Tooltip("net_income_pln:Q", format=",.0f")])
        pick = alt.selection_point(name="pick", fields=["unit_key", "year"], on="click")
        ev = st.altair_chart(_theme(bars.add_params(pick).properties(height=22 * yv["unit_key"].n_unique() * len(yrs) + 40)),
                             width="stretch", key="yield_chart", on_select="rerun")
        st.caption("Partial years are not annualised here; see Returns for annualised figures. Click a bar for the months behind it.")
        hit = (ev.selection.get("pick") or []) if hasattr(ev, "selection") else []
        if hit:
            h = hit[0]
            sel = {"unit_key": h.get("unit_key"), "year": h.get("year")}
            y_row = yv.filter((pl.col("unit_key") == sel["unit_key"]) & (pl.col("year") == int(sel["year"])))
            what = f"yield {y_row['yield_on_t0'][0]:.1%}" if y_row.height else "the selected bar"
            show_drill(pnl, sel, what, "yield_chart")
    table(ts, "portfolio_monthly")

# ----------------------------------------------------------------------------- pivot
with tab_pv:
    st.subheader("Slice and dice")
    st.caption("Group the monthly ledger by any dimensions, spread one across the columns, pick a measure. "
               "The chart follows the table; click a bar or a cell for the months behind it.")
    measures = [m for m in an.PIVOT_MEASURES if m in pnl.columns] + ["yield_on_t0"]
    c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
    rows_sel = c1.multiselect("Rows", an.PIVOT_DIMS, default=["unit_key"], key="pv_rows")
    cols_sel = c2.selectbox("Columns", ["(none)"] + an.PIVOT_DIMS, index=an.PIVOT_DIMS.index("year") + 1, key="pv_cols")
    val_sel = c3.selectbox("Measure", measures, index=0, key="pv_val", format_func=lambda m: m.replace("_", " "))
    agg_sel = c4.selectbox("Aggregate", list(an.PIVOT_AGGS), index=0, key="pv_agg", disabled=val_sel == "yield_on_t0",
                           help="yield on T0 is always Σ net income ÷ Σ T0 value of the group")
    with st.expander("Filters"):
        f1, f2, f3, f4 = st.columns(4)
        flt = {"year": f1.multiselect("Year", sorted(pnl["year"].unique().to_list()), key="pv_f_year"),
               "city": f2.multiselect("City", sorted(pnl["city"].drop_nulls().unique().to_list()), key="pv_f_city"),
               "manager": f3.multiselect("Manager", sorted(pnl["manager"].drop_nulls().unique().to_list()), key="pv_f_mgr"),
               "unit_key": f4.multiselect("Unit", sorted(pnl["unit_key"].unique().to_list()), key="pv_f_unit")}
    cols_dim = None if cols_sel == "(none)" else cols_sel
    if not rows_sel and cols_dim is None:
        st.info("Pick at least one row or column dimension.")
    else:
        agg = "sum" if val_sel == "yield_on_t0" else agg_sel
        long = an.pivot(pnl, rows_sel + ([cols_dim] if cols_dim else []), val_sel, agg, None, flt)
        wide = an.pivot(pnl, rows_sel, val_sel, agg, cols_dim, flt)
        # chart: one bar per row-group, coloured by the column dimension
        label_cols = rows_sel or [cols_dim]
        ch = long.with_columns(pl.concat_str([pl.col(c).cast(pl.Utf8).fill_null("-") for c in label_cols], separator=" · ").alias("label"))
        n_labels = ch["label"].n_unique()
        n_series = ch[cols_dim].n_unique() if cols_dim and rows_sel else 1
        fmt = ".1%" if val_sel == "yield_on_t0" else ",.0f"
        base = alt.Chart(ch.to_pandas()).mark_bar(cornerRadiusEnd=4, size=max(6, min(18, 320 // max(1, n_series))))
        enc = dict(y=alt.Y("label:N", title=None, sort="-x"),
                   x=alt.X(f"{val_sel}:Q", title=f"{agg} of {val_sel.replace('_', ' ')}", axis=alt.Axis(format=".0%" if val_sel == "yield_on_t0" else ",.0f")),
                   tooltip=[alt.Tooltip(c, type="nominal") for c in label_cols] + ([alt.Tooltip(f"{cols_dim}:N")] if cols_dim and rows_sel else [])
                   + [alt.Tooltip(f"{val_sel}:Q", format=fmt)])
        pick_fields = list(dict.fromkeys(label_cols + ([cols_dim] if cols_dim and rows_sel else [])))
        pick = alt.selection_point(name="pick", fields=pick_fields, on="click")
        if cols_dim and rows_sel:
            enc["color"] = alt.Color(f"{cols_dim}:N", legend=alt.Legend(title=cols_dim, orient="top"))
            enc["yOffset"] = alt.YOffset(f"{cols_dim}:N")
        else:
            base = base.mark_bar(cornerRadiusEnd=4, size=max(6, min(18, 320 // max(1, n_series))), color=BLUE)
        ch_alt = base.encode(**enc).add_params(pick).properties(height=min(900, 18 * n_labels * n_series + 60))
        ev = st.altair_chart(_theme(ch_alt), width="stretch", key="pivot_chart", on_select="rerun")
        hit = (ev.selection.get("pick") or []) if hasattr(ev, "selection") else []
        if hit:
            h = hit[0]
            show_drill(pnl, {k: h.get(k) for k in pick_fields}, f"the {val_sel.replace('_', ' ')} bar", "pivot_chart")
        table(wide, "pivot", pct=([c for c in wide.columns if c not in rows_sel] if val_sel == "yield_on_t0" else None), drill=pnl)

# ----------------------------------------------------------------------------- returns
with tab_r:
    st.subheader("Annual return per unit")
    mode = st.radio("Valuation model", ["Annual % change per city", "Paste a price/m² table"], horizontal=True, key="val_mode",
                    help="Simplified: value = T0 value × (1 + city growth)^(years since T0). Or the older price/m² table.")
    ledger_years = sorted(summary["year"].unique().to_list())
    vals = pl.DataFrame(schema=an.VALUE_SCHEMA)
    if mode == "Annual % change per city":
        cities = sorted(units["city"].drop_nulls().unique().to_list())
        st.session_state.setdefault("growth", {c: cfg.growth.get(c, cfg.growth.get(c.lower(), cfg.default_growth)) for c in cities})
        st.session_state.setdefault("default_growth", cfg.default_growth)
        g_cols = st.columns(max(1, min(6, len(cities) + 1)))
        growth: dict[str, float] = {}
        for i, city in enumerate(cities):
            growth[city] = g_cols[i % len(g_cols)].number_input(
                f"{city} % / year", value=float(st.session_state["growth"].get(city, cfg.default_growth)) * 100, step=0.5, format="%.1f",
                key=f"growth_{city}", help=f"Annual change in value for units in {city}. Applied from each unit's T0 year.") / 100
        dflt = g_cols[len(cities) % len(g_cols)].number_input(
            "other / unknown city % / year", value=float(st.session_state["default_growth"]) * 100, step=0.5, format="%.1f", key="growth_default") / 100
        st.session_state["growth"], st.session_state["default_growth"] = growth, dflt
        since_t0 = st.checkbox("Show every year since the earliest T0", value=False, key="val_since_t0")
        t0_min = units["acquired_on"].drop_nulls().dt.year().min() if "acquired_on" in units.columns and units["acquired_on"].drop_nulls().len() else None
        first = min(ledger_years) - 1 if ledger_years else dt.date.today().year
        if since_t0 and t0_min is not None:
            first = min(first, int(t0_min))
        years = list(range(first, (max(ledger_years) if ledger_years else dt.date.today().year) + 1))
        vals = an.growth_values(units, growth, years, dflt)
        st.markdown("**Unit values under these rates** — updates as you type; check the % against the numbers here")
        table(an.values_matrix(vals, units), "unit_values_live")
        st.caption("value(year) = T0 value × (1 + city %)^(year − T0 year). Garages follow their city. Years before a unit's T0 are blank. "
                   "Put the rates in config.toml under [valuations.growth] to keep them.")
    else:
        c1, c2 = st.columns([2, 3])
        with c1:
            st.session_state["prices_text"] = st.text_area(
                "Price per m² by year — paste cells from a sheet", value=st.session_state["prices_text"], height=160,
                help="Two columns: year, PLN/m². Or a header row naming cities (and 'garaz' for a garage price), then one row per year.")
            st.caption("Flat value = price/m² × area. Garage = T0 value scaled by the city's price index, unless a 'garaz' column gives a price.")
        try:
            prices = an.parse_price_table(st.session_state["prices_text"])
        except ValueError as ex:
            st.error(str(ex))
            prices = an.parse_price_table("")
        with c2:
            if prices.height:
                table(prices, "prices")
            else:
                st.info("Paste a valuation table to compute returns. Yields on the T0 purchase value are on the Portfolio tab meanwhile.")
        if prices.height:
            vals = an.unit_values(units, prices, cfg.garage_value_index)
    if vals.height:
        ret = an.returns(summary, units, vals, tax_y, cfg)
        tot = an.portfolio_returns(ret)
        metric = st.radio("Show", ["total_return", "income_return", "capital_return", "total_return_after_tax", "income_return_after_tax"],
                          horizontal=True, format_func=lambda m: m.replace("_", " "))
        mat = an.returns_matrix(ret, tot, metric)
        years_cols = [c for c in mat.columns if c != "unit_key"]
        table(mat, f"returns_{metric}", pct=years_cols, drill=pnl)
        st.caption("income return = annualised net income ÷ value at the start of the year · capital return = change in value · "
                   "after-tax allocates the year's ryczałt to units pro rata to their taxable profit. Partial years are annualised. "
                   "Click a cell for the months behind it.")
        with st.expander("Per unit and year, all columns"):
            table(ret.select(["unit_key", "year", "months", "value_pln", "value_prev_pln", "net_income_pln", "tax_alloc_pln", "net_after_tax_pln",
                              "income_return", "capital_return", "total_return", "total_return_after_tax"]), "returns_detail",
                  pct=["income_return", "capital_return", "total_return", "total_return_after_tax"], drill=pnl)
        with st.expander("Portfolio by year"):
            table(tot, "returns_portfolio", pct=["income_return", "income_return_after_tax", "capital_return", "total_return", "total_return_after_tax"])
        with st.expander("Unit values used"):
            table(vals, "unit_values")

# ----------------------------------------------------------------------------- units
with tab_u:
    st.subheader("Per unit")
    ukeys = sorted(pnl["unit_key"].unique().to_list())
    sel = st.selectbox("Unit", ukeys)
    u = pnl.filter(pl.col("unit_key") == sel).sort(["year", "month"])
    info = units.filter(pl.col("unit_key") == sel).row(0, named=True) if units.filter(pl.col("unit_key") == sel).height else {}
    st.caption(" · ".join(f"{k}: {info[k]}" for k in ["label", "kind", "city", "manager", "currency", "area_m2", "value_t0", "acquired_on"] if info.get(k) is not None))
    a_series = e.arrears_series(pnl).filter(pl.col("unit_key") == sel)
    ch = alt.Chart(a_series.to_pandas()).mark_area(line={"color": BLUE, "strokeWidth": 2}, color=BLUE, opacity=0.15).encode(
        x=alt.X("period:T", title=None, axis=alt.Axis(format="%b %y")), y=alt.Y("cum_balance_pln:Q", title="cumulative balance, PLN"),
        tooltip=[alt.Tooltip("period:T", format="%Y-%m"), alt.Tooltip("balance_pln:Q", format=",.2f"), alt.Tooltip("cum_balance_pln:Q", format=",.2f")])
    st.altair_chart(_theme(ch.properties(height=180)), width="stretch")
    table(u.select(["year", "month", "contract_rent", "media_advance", "tenant_payment", "mgmt_invoice", "hoa_fee", "electricity", "repairs",
                    "non_tax_costs", "transfer", "expected_transfer", "balance", "taxable_profit", "media_result", "net_income", "fx"]), f"unit_{sel}")
    st.subheader("Yearly summary, all units")
    table(summary.select(["unit_key", "year", "months", "occupied_months", "contract_rent_pln", "transfer_pln", "expected_transfer_pln", "balance_pln",
                          "mgmt_pln", "repairs_pln", "non_tax_costs_pln", "hoa_pln", "electricity_pln", "media_result_pln", "taxable_profit_pln",
                          "net_income_pln", "yield_on_t0"]), "unit_year_summary", pct=["yield_on_t0"], drill=pnl)

# ----------------------------------------------------------------------------- problems
with tab_pr:
    st.subheader("Problems")
    st.caption("Arrears, underpayment by manager, vacancies, gaps and duplicates, units missing from a tab, formula mismatches, "
               "media advances below actual bills, rents not indexed, missing FX. Thresholds and muted kinds are in config.toml.")
    counts = probs.group_by(["kind", "severity"]).len().sort(["severity", "kind"])
    if counts.height:
        bars = alt.Chart(counts.to_pandas()).mark_bar(cornerRadiusEnd=4, size=14).encode(
            y=alt.Y("kind:N", title=None, sort="-x"), x=alt.X("len:Q", title="findings"),
            color=alt.Color("severity:N", scale=alt.Scale(domain=list(STATUS), range=list(STATUS.values())), legend=alt.Legend(orient="top", title=None)),
            tooltip=["kind:N", "severity:N", "len:Q"])
        st.altair_chart(_theme(bars.properties(height=30 * counts.height + 60)), width="stretch")
    c1, c2, c3 = st.columns(3)
    sev = c1.multiselect("Severity", ["high", "medium", "low"], default=["high", "medium"])
    kinds = c2.multiselect("Kind", sorted(probs["kind"].unique().to_list()))
    uf = c3.multiselect("Unit", sorted(probs["unit_key"].unique().to_list()))
    f = probs.filter(pl.col("severity").is_in(sev))
    if kinds:
        f = f.filter(pl.col("kind").is_in(kinds))
    if uf:
        f = f.filter(pl.col("unit_key").is_in(uf))
    table(f, "problems", height=500)
    st.subheader("Arrears by unit")
    table(arr, "arrears", drill=pnl)
    st.subheader("Shortfall by manager")
    by_mgr = pnl.filter(pl.col("balance_pln") < -0.005).group_by(pl.col("manager").fill_null("-")).agg([
        pl.len().alias("months_short"), pl.col("balance_pln").sum().round(2).alias("shortfall_pln")]).sort("shortfall_pln")
    table(by_mgr, "shortfall_by_manager", drill=pnl)

# ----------------------------------------------------------------------------- tax & ops
with tab_t:
    st.subheader("Ryczałt on rental income")
    st.caption(f"{cfg.tax.rate_low:.1%} up to {cfg.tax.threshold_pln:,.0f} PLN of taxable income per year across all units, {cfg.tax.rate_high:.1%} above. "
               f"Base = transfer + repairs + management fee (the sheet's Zysk-do-podatku), foreign units converted at the NBP rate.")
    table(tax_y, "tax_by_year", pct=["effective_rate"])
    st.subheader("Monthly advances")
    sched = e.tax_schedule(pnl, cfg.tax)
    yr = st.selectbox("Year", sorted(sched["year"].unique().to_list(), reverse=True))
    s = sched.filter(pl.col("year") == yr)
    bars = alt.Chart(s.to_pandas()).mark_bar(cornerRadiusEnd=4, size=18, color=BLUE).encode(
        x=alt.X("month:O", title=None), y=alt.Y("advance_pln:Q", title="advance due, PLN"),
        tooltip=["month:O", alt.Tooltip("base_pln:Q", format=",.0f"), alt.Tooltip("cum_base_pln:Q", format=",.0f"), alt.Tooltip("advance_pln:Q", format=",.2f"), "due_on:T", "band:N"])
    st.altair_chart(_theme(bars.properties(height=200)), width="stretch")
    table(s, f"tax_schedule_{yr}")
    st.subheader("Expected cash next month, by manager")
    st.caption("Contract rent minus management fee for the latest month on file; what should arrive if nothing changes.")
    table(e.expected_cash(pnl), "expected_cash")
    st.subheader("Taxable base by unit and year")
    table(summary.select(["unit_key", "year", "taxable_profit_pln"]).pivot(on="year", index="unit_key", values="taxable_profit_pln"), "tax_base_by_unit", drill=pnl)

# ----------------------------------------------------------------------------- trades
with tab_tr:
    st.subheader("Securities")
    if trades.height == 0:
        st.info("No Trades tab in the workbook. Add one with columns Date, Ticker, Side, Qty, Price, Fee, Currency, Account.")
    else:
        c1, c2 = st.columns(2)
        method = c1.radio("Cost basis", ["fifo", "lifo", "average"], horizontal=True)
        fees = c2.radio("Fees", ["capitalize", "expense"], horizontal=True, help="capitalize: buy fees into cost, sell fees off proceeds; expense: each fee is its own realised loss")
        try:
            realized, pos, lots = e.cost_basis(trades, method, fees)
        except ValueError as ex:
            st.error(str(ex))
            realized = pos = lots = pl.DataFrame()
        if pos.height:
            latest_q = quotes_t.sort("ts").group_by("ticker").agg([pl.col("price").last(), pl.col("ts").last()]) if quotes_t.height else pl.DataFrame({"ticker": [], "price": []}, schema={"ticker": pl.Utf8, "price": pl.Float64})
            unre = e.unrealized_pnl(pos, latest_q.select(["ticker", "price"]))
            m1, m2, m3 = st.columns(3)
            m1.metric("Realised PnL", f"{realized['realized_pnl'].sum():,.2f}")
            m2.metric("Unrealised PnL", f"{unre['unrealized_pnl'].sum():,.2f}" if unre["unrealized_pnl"].drop_nulls().len() else "— (no quotes)")
            m3.metric("Cost basis held", f"{pos['cost_basis'].sum():,.2f}")
            st.caption(f"Quotes as of {latest_q['ts'].max()}" if quotes_t.height else "No quotes yet — Refresh quotes in the sidebar (sends ticker symbols only).")
            table(unre, "positions")
        st.subheader("Realised, by trade")
        table(realized, "realized")
        with st.expander("Open lots"):
            table(lots, "lots")
        with st.expander("Trades as parsed"):
            table(trades, "trades")

# ----------------------------------------------------------------------------- data
with tab_d:
    st.subheader("Data quality")
    st.caption("Rows where the sheet's stored formula results differ from the recomputed ones.")
    table(disc, "discrepancies")
    st.subheader("Sync log")
    table(tables.get("sync_log", pl.DataFrame()).sort("ts", descending=True) if "sync_log" in tables else pl.DataFrame(), "sync_log")
    st.subheader("Units")
    table(units, "units")
    st.subheader("FX rates fetched")
    table(fx_rates, "fx_rates")
    if st.session_state["notes"]:
        st.subheader("Session notes")
        st.code("\n".join(st.session_state["notes"][-30:]))
    st.caption(f"Vault: {vault.path} · key in keychain service '{cfg.keyring_service}' · server {address}")
