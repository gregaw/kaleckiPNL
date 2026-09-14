import datetime as dt
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


def test_growth_values_compounds_from_t0_year():
    units = pl.DataFrame({"unit_key": ["F", "G", "H"], "kind": ["mieszkanie", "garaz", "mieszkanie"], "city": ["Krakow", "Krakow", None],
                          "value_t0": [100_000.0, 10_000.0, 50_000.0],
                          "acquired_on": [dt.date(2023, 5, 1), dt.date(2024, 1, 1), None]})
    v = an.growth_values(units, {"krakow": 0.10}, [2023, 2024, 2025], default_growth=0.0)
    f = v.filter(pl.col("unit_key") == "F")
    assert f["year"].to_list() == [2023, 2024, 2025]
    assert f["value_pln"].to_list() == [100_000.0, 110_000.0, 121_000.0]
    g = v.filter(pl.col("unit_key") == "G")
    assert g["year"].to_list() == [2024, 2025] and g["value_pln"].to_list() == [10_000.0, 11_000.0]  # nothing before T0
    h = v.filter(pl.col("unit_key") == "H")
    assert h["value_pln"].to_list() == [50_000.0] * 3  # unknown city, default 0 %, valued from the first year asked
    assert "10.0 %" in f["value_source"][0]
    m = an.values_matrix(v, units)
    assert m.columns == ["unit_key", "city", "kind", "value_t0", "2023", "2024", "2025"]
    assert m.filter(pl.col("unit_key") == "TOTAL")["2025"][0] == 121_000 + 11_000 + 50_000
    assert an.growth_values(units, {}, []).height == 0


def test_growth_values_feed_returns(parsed):
    p = e.monthly_pnl(parsed.ledger, parsed.units)
    s = e.unit_year_summary(p, parsed.units)
    years = sorted(s["year"].unique().to_list())
    v = an.growth_values(parsed.units, {"Krakow": 0.05}, years, 0.02)
    r = an.returns(s, parsed.units, v, e.tax_estimate(p, Config().tax), Config())
    later = r.filter(pl.col("year") == years[-1]).filter(pl.col("capital_return").is_not_null())
    assert later.height > 0
    for x in later["capital_return"].to_list():  # values are rounded to whole PLN
        assert min(abs(x - 0.05), abs(x - 0.02)) < 1e-4


def test_pivot_and_drill(parsed):
    p = e.monthly_pnl(parsed.ledger, parsed.units)
    wide = an.pivot(p, ["city"], "net_income_pln", "sum", "year")
    years = sorted(str(y) for y in p["year"].unique().to_list())
    assert wide.columns == ["city"] + years + ["TOTAL"]
    assert wide["TOTAL"].sum() == pytest.approx(p["net_income_pln"].sum(), abs=0.05)
    long = an.pivot(p, ["city", "year"], "net_income_pln", "sum")
    assert long.columns == ["city", "year", "net_income_pln"] and long.height == wide.height * len(years)
    cnt = an.pivot(p, ["unit_key"], "net_income_pln", "count", filters={"year": [p["year"].min()]})
    assert cnt["net_income_pln"].max() <= 12
    y = an.pivot(p, ["unit_key"], "yield_on_t0", "sum", "year")
    u = parsed.units.filter(pl.col("value_t0") > 0).row(0, named=True)["unit_key"]
    s = e.unit_year_summary(p, parsed.units).filter((pl.col("unit_key") == u) & (pl.col("year") == int(years[0])))
    assert y.filter(pl.col("unit_key") == u)[years[0]][0] == pytest.approx(s["yield_on_t0"][0])
    no_rows = an.pivot(p, [], "net_income_pln", "sum", "year")
    assert no_rows.height == 1 and "TOTAL" in no_rows.columns
    with pytest.raises(ValueError):
        an.pivot(p, [], "net_income_pln", "sum")
    d = an.drill(p, {"unit_key": u, "year": years[0], "TOTAL": 1.0, "not_a_column": "x"})
    assert d.height == s["months"][0] and set(d["unit_key"].to_list()) == {u}


def test_apply_filters_and_cell_selection(parsed):
    p = e.monthly_pnl(parsed.ledger, parsed.units)
    u = parsed.units.filter(pl.col("in_ledger")).row(0, named=True)["unit_key"]
    scope = an.apply_filters(p, {"unit_key": [u], "year": [], "not_a_column": ["x"]})
    assert set(scope["unit_key"].to_list()) == {u}
    # the pivot on the filtered scope equals the pivot with the same filters
    a = an.pivot(p, ["unit_key"], "net_income_pln", "sum", "year", {"unit_key": [u]})
    b = an.pivot(scope, ["unit_key"], "net_income_pln", "sum", "year")
    assert a.equals(b)
    # clicking a year cell of that pivot drills into the filtered unit only
    wide = an.pivot(scope, ["city"], "net_income_pln", "sum", "year")
    year = [c for c in wide.columns if c.isdigit()][0]
    sel, what = an.cell_selection(wide, (0, year))
    assert sel["year"] == int(year) and sel["city"] == wide["city"][0] and what.startswith(f"{year} = ")
    rows = an.drill(scope, sel)
    assert rows.height and set(rows["unit_key"].to_list()) == {u} and set(rows["year"].to_list()) == {int(year)}
    # a stale click after the table changed shape is ignored instead of raising
    assert an.cell_selection(wide, (0, "XXX")) is None
    assert an.cell_selection(wide, (wide.height, year)) is None
    assert an.cell_selection(wide, None) is None
    assert an.cell_selection(wide, (0, "TOTAL"))[0].get("year") is None


def test_values_matrix_keeps_units_without_values_and_valuation_gaps(parsed):
    units = parsed.units
    vals = an.growth_values(units, {}, [2024, 2025], 0.0)
    m = an.values_matrix(vals, units)
    assert m.height == units.height + 1 and m["value_t0"].null_count() == units["value_t0"].null_count()
    gaps = an.valuation_gaps(units)
    ledger_only = units.filter(~pl.col("in_reference"))["unit_key"].to_list()
    assert set(gaps["unit_key"].to_list()) == set(ledger_only)
    assert all("not in Reference" in w for w in gaps["why"].to_list())
    blank = units.with_columns(pl.when(pl.col("in_reference")).then(None).otherwise(pl.col("value_t0")).alias("value_t0"))
    g2 = an.valuation_gaps(blank)
    assert g2.height == units.height and (g2.filter(pl.col("in_reference"))["why"].str.contains("empty").all())
