import polars as pl
import pytest

import engine as e
from kalecki import analysis as an
from kalecki.config import AnalysisConfig, Config


def test_parse_price_table_two_columns():
    p = an.parse_price_table("2024\t12 000\n2025 13000,5\n")
    assert p["year"].to_list() == [2024, 2025]
    assert p["price_m2"].to_list() == [12000.0, 13000.5]
    assert p["city"].to_list() == [None, None]


def test_parse_price_table_with_cities_and_garage():
    p = an.parse_price_table("rok;Krakow;Warszawa;garaz\n2024;12000;15000;80000\n2025;13000;16000;90000")
    assert p.filter(pl.col("city") == "Krakow")["price_m2"].to_list() == [12000.0, 13000.0]
    assert p.filter(pl.col("year") == 2025)["garage_price"].unique().to_list() == [90000.0]
    assert p.filter(pl.col("city").is_null()).height == 0


def test_parse_price_table_rejects_unnamed_columns():
    with pytest.raises(ValueError):
        an.parse_price_table("2024\t1\t2")
    assert an.parse_price_table("").height == 0


def test_unit_values_flat_and_garage():
    units = pl.DataFrame({"unit_key": ["F", "G", "H"], "kind": ["mieszkanie", "garaz", "garaz"], "city": ["Krakow", "Krakow", "Gdansk"],
                          "area_m2": [50.0, None, None], "value_t0": [1.0, 100_000.0, 50_000.0]})
    prices = an.parse_price_table("rok\tKrakow\tGdansk\n2024\t10000\t8000\n2025\t11000\t8800")
    v = an.unit_values(units, prices, True)
    assert v.filter(pl.col("unit_key") == "F")["value_pln"].to_list() == [500_000.0, 550_000.0]
    assert v.filter(pl.col("unit_key") == "G")["value_pln"].to_list() == [100_000.0, 110_000.0]
    assert v.filter(pl.col("unit_key") == "H")["value_pln"].to_list() == [50_000.0, 55_000.0]
    flat = an.unit_values(units, prices, False)
    assert flat.filter(pl.col("unit_key") == "G")["value_pln"].to_list() == [100_000.0, 100_000.0]


def test_returns_arithmetic():
    units = pl.DataFrame({"unit_key": ["F"], "kind": ["mieszkanie"], "city": [None], "area_m2": [50.0], "value_t0": [400_000.0]})
    prices = an.parse_price_table("2024\t10000\n2025\t11000")
    vals = an.unit_values(units, prices)
    summary = pl.DataFrame({"unit_key": ["F", "F"], "year": [2024, 2025], "months": [12, 6], "partial_year": [False, True],
                            "taxable_profit_pln": [30_000.0, 15_000.0], "net_income_pln": [24_000.0, 12_000.0]})
    tax = pl.DataFrame({"year": [2024, 2025], "base_pln": [30_000.0, 15_000.0], "tax_pln": [2550.0, 1275.0]})
    r = an.returns(summary, units, vals, tax, Config())
    y24, y25 = r.row(0, named=True), r.row(1, named=True)
    assert y24["value_pln"] == 500_000 and y24["value_prev_pln"] is None and y24["value_base_pln"] == 500_000
    assert y24["income_return"] == pytest.approx(24_000 / 500_000) and y24["capital_return"] is None
    assert y24["total_return"] == pytest.approx(24_000 / 500_000)
    assert y25["net_income_ann_pln"] == 24_000  # 6 months annualised
    assert y25["capital_return"] == pytest.approx(0.1)
    assert y25["total_return"] == pytest.approx(24_000 / 500_000 + 0.1)
    assert y25["tax_alloc_pln"] == 1275.0 and y25["net_after_tax_pln"] == 10_725.0
    tot = an.portfolio_returns(r)
    assert tot.filter(pl.col("year") == 2025)["total_return"][0] == pytest.approx(y25["total_return"])
    m = an.returns_matrix(r, tot, "total_return")
    assert m.columns == ["unit_key", "2024", "2025"] and m["unit_key"].to_list() == ["F", "TOTAL"]


@pytest.fixture(scope="module")
def probs(parsed):
    p = e.monthly_pnl(parsed.ledger, parsed.units)
    return an.problems(p, parsed.units, e.arrears(p), e.discrepancies(p), AnalysisConfig())


def test_problems_find_planted_issues(probs):
    kinds = dict(probs.group_by("kind").len().iter_rows())
    assert kinds["arrears"] == 1
    assert kinds["formula_mismatch"] == 1
    assert kinds["missing_month"] == 1
    assert kinds["not_in_reference"] == 1 and kinds["not_in_ledger"] == 1
    assert kinds["vacancy"] == 3
    assert kinds["rent_unchanged"] >= 1 and kinds["underpaid"] >= 4
    assert set(probs["severity"].unique().to_list()) <= {"high", "medium", "low"}
    assert probs["severity"][0] == "high"  # sorted high first


def test_muted_kinds(parsed):
    p = e.monthly_pnl(parsed.ledger, parsed.units)
    pr = an.problems(p, parsed.units, e.arrears(p), e.discrepancies(p), AnalysisConfig(muted=["underpaid", "media_shortfall", "vacancy"]))
    assert not set(pr["kind"].unique().to_list()) & {"underpaid", "media_shortfall", "vacancy"}


def test_duplicate_and_gap_detection():
    base = {c: [None] * 3 for c in ["manager", "city", "address", "unit_no", "owner"]}
    ledger = pl.DataFrame({"unit_key": ["A"] * 3, "year": [2025] * 3, "month": [1, 1, 4], "transfer": [1.0] * 3, "contract_rent": [1.0] * 3,
                           "row_no": [1, 2, 3], **base}).with_columns(
        *[pl.lit(None, dtype=pl.Float64).alias(c) for c in ["media_advance", "tenant_payment", "mgmt_invoice", "hoa_fee", "electricity", "repairs",
                                                            "expected_transfer_sheet", "balance_sheet", "non_tax_costs", "taxable_profit_sheet", "fx_rate"]],
        pl.lit(None, dtype=pl.Date).alias("fx_date"))
    units = pl.DataFrame({"unit_key": ["A"], "kind": ["mieszkanie"], "currency": ["PLN"], "value_t0": [1.0], "area_m2": [1.0], "in_ledger": [True], "in_reference": [True]})
    p = e.monthly_pnl(ledger, units)
    pr = an.problems(p, units, e.arrears(p), e.discrepancies(p))
    kinds = dict(pr.group_by("kind").len().iter_rows())
    assert kinds["duplicate_row"] == 1 and kinds["missing_month"] == 2
