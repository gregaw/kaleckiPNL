import polars as pl

from kalecki.parse import build_units, norm, parse_ledger, parse_reference, parse_trades
from kalecki.testdata import make_workbook


def test_fixture_shapes(parsed):
    assert parsed.ledger.height == 215
    assert parsed.units.height == 13
    assert parsed.trades.height == 40
    assert parsed.warnings == []


def test_header_found_by_content_and_polish_folded():
    assert norm("Wpłata Najemcy") == "wplata najemcy"
    rows = [(None,), (None, "Lokal", "Rok", "mc", "Czynsz zgodnie z umową", "Przelew", "Przelew spodziewany", "kurs NBP"),
            (None, "K, A 1/ 2", 2025.0, 3.0, "1 200,50", 1000, "=J3-M3", '=nbp_rateT1("GBP",V3)'),
            (None, None, None, None, None, None, None, None)]
    df, warnings = parse_ledger(rows)
    assert df.height == 1
    r = df.row(0, named=True)
    assert r["contract_rent"] == 1200.5 and r["transfer"] == 1000.0
    assert r["expected_transfer_sheet"] is None and r["fx_rate"] is None  # formula strings, no cached value
    assert r["year"] == 2025 and r["month"] == 3 and r["row_no"] == 3


def test_mixed_unit_numbers_kept_as_text(parsed):
    nos = set(parsed.ledger["unit_no"].unique().to_list())
    assert any(n.startswith("g") for n in nos) and any(n.endswith("MP") for n in nos)
    assert all("." not in n for n in nos)  # 14.0 -> '14'


def test_reference_and_union(parsed):
    u = parsed.units
    assert u.filter(pl.col("in_reference") & ~pl.col("in_ledger")).height == 1
    assert u.filter(pl.col("in_ledger") & ~pl.col("in_reference")).height == 1
    assert set(u["kind"].unique().to_list()) == {"mieszkanie", "garaz"}
    assert u.filter(pl.col("currency") == "GBP").height == 1  # inferred from kurs NBP


def test_currency_override():
    ledger = pl.DataFrame({"unit_key": ["A"], "year": [2025], "month": [1]}).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("fx_rate"), pl.lit(None, dtype=pl.Date).alias("fx_date"),
        *[pl.lit(None, dtype=pl.Utf8).alias(c) for c in ["city", "address", "unit_no", "owner", "manager"]])
    ref = parse_reference(None)
    u = build_units(ledger, ref, {"A": "EUR"})
    assert u["currency"].to_list() == ["EUR"]


def test_trades_optional_and_defaults():
    assert parse_trades(None).height == 0
    rows = [("Date", "Ticker", "Side", "Qty", "Price"), ("2025-01-02", "acme", None, 3, "10,5")]
    t = parse_trades(rows)
    assert t.row(0, named=True)["side"] == "BUY" and t.row(0, named=True)["price"] == 10.5 and t.row(0, named=True)["fee"] == 0.0


def test_generator_is_deterministic(tmp_path):
    a, _ = make_workbook(5, 4, seed=1)
    b, _ = make_workbook(5, 4, seed=1)
    ra = [r for r in a["All-Data"].iter_rows(values_only=True)]
    rb = [r for r in b["All-Data"].iter_rows(values_only=True)]
    assert ra == rb
