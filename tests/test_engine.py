import datetime as dt

import polars as pl
import pytest

import engine as e
from kalecki.config import AnalysisConfig, TaxConfig


@pytest.fixture(scope="module")
def pnl(parsed):
    return e.monthly_pnl(parsed.ledger, parsed.units, AnalysisConfig())


def test_sheet_formulas_reproduced(pnl):
    d = e.discrepancies(pnl)
    # the generator plants exactly one deliberate mismatch, in taxable_profit
    assert d.height == 1 and d.row(0, named=True)["column"] == "taxable_profit"
    r = pnl.filter(pl.col("expected_transfer_sheet").is_not_null()).row(0, named=True)
    assert r["expected_transfer"] == pytest.approx((r["contract_rent"] or 0) - (r["mgmt_invoice"] or 0) - (r["repairs"] or 0) - (r["non_tax_costs"] or 0))
    assert r["balance"] == pytest.approx((r["transfer"] or 0) - r["expected_transfer"])
    assert r["taxable_profit"] == pytest.approx((r["transfer"] or 0) + (r["repairs"] or 0) + (r["mgmt_invoice"] or 0))


def test_foreign_unit_converted_to_pln(pnl):
    g = pnl.filter(pl.col("currency") == "GBP")
    assert g.height > 0
    r = g.row(0, named=True)
    assert r["fx"] == r["fx_rate"] and r["taxable_profit_pln"] == pytest.approx(r["taxable_profit"] * r["fx_rate"], abs=0.01)
    assert (pnl.filter(pl.col("currency") == "PLN")["fx"] == 1.0).all()


def test_fx_carried_forward_within_unit():
    ledger = pl.DataFrame({"unit_key": ["G", "G", "G"], "year": [2025] * 3, "month": [1, 2, 3], "transfer": [10.0, 10.0, 10.0],
                           "fx_rate": [None, 5.0, None], "fx_date": [None] * 3}).with_columns(
        *[pl.lit(None, dtype=pl.Float64).alias(c) for c in ["contract_rent", "media_advance", "tenant_payment", "mgmt_invoice", "hoa_fee",
                                                            "electricity", "repairs", "expected_transfer_sheet", "balance_sheet", "non_tax_costs", "taxable_profit_sheet"]],
        *[pl.lit(None, dtype=pl.Utf8).alias(c) for c in ["manager", "city", "address", "unit_no", "owner"]], pl.lit(1).alias("row_no"))
    units = pl.DataFrame({"unit_key": ["G"], "kind": ["mieszkanie"], "currency": ["GBP"], "value_t0": [1.0], "area_m2": [1.0]})
    p = e.monthly_pnl(ledger, units)
    assert p["fx"].to_list() == [5.0, 5.0, 5.0]
    assert p["transfer_pln"].to_list() == [50.0, 50.0, 50.0]


def test_arrears_detects_planted_unit(pnl):
    a = e.arrears(pnl)
    worst = a.row(0, named=True)
    assert worst["outstanding_pln"] < -1000 and worst["months_short"] == 4


def test_ryczalt_bands():
    t = TaxConfig()
    assert e.ryczalt(0, t) == 0 and e.ryczalt(-5, t) == 0
    assert e.ryczalt(100_000, t) == 8500.0
    assert e.ryczalt(120_000, t) == pytest.approx(8500 + 2500)
    t2 = TaxConfig(threshold_pln=200_000)
    assert e.ryczalt(150_000, t2) == pytest.approx(12_750)


def test_tax_schedule_advances_sum_to_year(pnl):
    t = TaxConfig()
    sched = e.tax_schedule(pnl, t)
    year = e.tax_estimate(pnl, t)
    for y in year["year"].to_list():
        s = sched.filter(pl.col("year") == y)
        assert s["advance_pln"].sum() == pytest.approx(year.filter(pl.col("year") == y)["tax_pln"][0], abs=0.05)
        assert s.row(-1, named=True)["due_on"].day == 20
    dec = sched.filter(pl.col("month") == 12)
    if dec.height:
        assert dec.row(0, named=True)["due_on"] == dt.date(dec.row(0, named=True)["year"] + 1, 1, 20)


def test_unit_year_summary_and_yield(pnl, parsed):
    s = e.unit_year_summary(pnl, parsed.units)
    full = s.filter(~pl.col("partial_year"))
    assert (full["months"] == 12).all()
    r = full.filter(pl.col("value_t0").is_not_null()).row(0, named=True)
    assert r["yield_on_t0"] == pytest.approx(r["net_income_pln"] / r["value_t0"])


def test_media_flags_change_net_income(parsed):
    base = e.monthly_pnl(parsed.ledger, parsed.units, AnalysisConfig(owner_pays_hoa=False, owner_pays_electricity=False))
    assert (base["media_result"] == 0).all()
    assert ((base["net_income"] - (base["taxable_profit"] - base["non_tax_costs"].fill_null(0))).abs() < 0.006).all()


# ----------------------------------------------------------------------------- trades

def _trades(rows):
    return pl.DataFrame(rows, schema={"trade_id": pl.Int32, "ts": pl.Datetime("us"), "ticker": pl.Utf8, "side": pl.Utf8, "qty": pl.Float64,
                                      "price": pl.Float64, "fee": pl.Float64, "currency": pl.Utf8, "account": pl.Utf8}, orient="row")


T = dt.datetime(2025, 1, 1)
BOOK = _trades([
    (1, T, "X", "BUY", 10, 100.0, 5.0, "PLN", "a"),
    (2, T + dt.timedelta(days=1), "X", "BUY", 10, 120.0, 5.0, "PLN", "a"),
    (3, T + dt.timedelta(days=2), "X", "SELL", 15, 130.0, 5.0, "PLN", "a"),
])


def test_fifo_hand_computed():
    realized, pos, lots = e.cost_basis(BOOK, "fifo", "capitalize")
    # first lot: 10 @ 100.5 (fee in); second: 10 @ 120.5. Sell 15: 10*100.5 + 5*120.5 = 1607.5
    r = realized.row(0, named=True)
    assert r["cost"] == pytest.approx(1607.5)
    assert r["proceeds"] == pytest.approx(15 * 130 - 5)
    assert r["realized_pnl"] == pytest.approx(1945 - 1607.5)
    assert pos.row(0, named=True)["qty"] == 5 and pos.row(0, named=True)["avg_cost"] == pytest.approx(120.5)
    assert lots.height == 1


def test_lifo_and_average():
    r_lifo, _, _ = e.cost_basis(BOOK, "lifo", "capitalize")
    assert r_lifo.row(0, named=True)["cost"] == pytest.approx(10 * 120.5 + 5 * 100.5)
    r_avg, pos, _ = e.cost_basis(BOOK, "average", "capitalize")
    assert r_avg.row(0, named=True)["cost"] == pytest.approx(15 * 110.5)
    assert pos.row(0, named=True)["avg_cost"] == pytest.approx(110.5)


def test_fees_expensed():
    realized, _, _ = e.cost_basis(BOOK, "fifo", "expense")
    fee_rows = realized.filter(pl.col("qty") == 0)
    assert fee_rows.height == 2 and fee_rows["realized_pnl"].to_list() == [-5.0, -5.0]
    sell = realized.filter(pl.col("qty") > 0).row(0, named=True)
    assert sell["cost"] == pytest.approx(10 * 100 + 5 * 120) and sell["realized_pnl"] == pytest.approx(15 * 130 - 1600 - 5)


def test_oversell_rejected():
    with pytest.raises(ValueError):
        e.cost_basis(_trades([(1, T, "X", "SELL", 1, 10.0, 0.0, "PLN", "a")]))


def test_unrealized(parsed):
    _, pos, _ = e.cost_basis(parsed.trades)
    q = pl.DataFrame({"ticker": [pos["ticker"][0]], "price": [1000.0]})
    u = e.unrealized_pnl(pos, q)
    r = u.row(0, named=True)
    assert r["market_value"] == pytest.approx(r["qty"] * 1000) and r["unrealized_pnl"] == pytest.approx(r["market_value"] - r["cost_basis"])
    assert u.filter(pl.col("price").is_null()).height == pos.height - 1


def test_open_db_loads_all_tables(parsed):
    con = e.open_db({"units": parsed.units, "rent_ledger": parsed.ledger, "trades": parsed.trades})
    assert con.execute("select count(*) from rent_ledger").fetchone()[0] == 215
    assert con.execute("select count(*) from units").fetchone()[0] == 13
