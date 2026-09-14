"""The dashboard itself, run headless with Streamlit's AppTest against the synthetic vault.
No Keychain (MemoryKeyStore), no network (offline), no browser. Cell clicks on dataframes are
not simulated by AppTest; that path is covered by `analysis.cell_selection` tests."""
import pathlib

import polars as pl
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import sync as sy
from kalecki import config as kcfg
from kalecki import store
from kalecki.store import MemoryKeyStore, Vault

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"


@pytest.fixture(scope="module")
def app_env(tmp_path_factory, sample_bytes):
    tmp = tmp_path_factory.mktemp("vault")
    ks = MemoryKeyStore()
    cfg = kcfg.Config(vault=str(tmp / "kalecki.vault"), offline=True, growth={"Krakow": 0.05})
    sy.ingest(sample_bytes, "local:sample.xlsx", cfg, Vault(cfg.vault, ks), force=True)
    real_ks, real_load = store.KeyStore, kcfg.load
    store.KeyStore = lambda service="kaleckiPNL": ks
    kcfg.load = lambda path="config.toml": cfg
    st.config.set_option("server.address", "127.0.0.1")
    yield cfg
    store.KeyStore, kcfg.load = real_ks, real_load


def run_app(**kw) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120)
    return at.run(**kw)


def _errors(at: AppTest) -> list[str]:
    return [str(e.value) for e in at.exception] + [str(e.value) for e in at.error]


def test_every_tab_renders(app_env):
    at = run_app()
    assert not _errors(at), _errors(at)
    assert len(at.tabs) == 8 and len(at.dataframe) > 10


def test_refuses_non_loopback(app_env):
    st.config.set_option("server.address", "0.0.0.0")
    try:
        at = run_app()
        assert any("Refusing" in str(e.value) for e in at.error) and not at.tabs
    finally:
        st.config.set_option("server.address", "127.0.0.1")


def test_pivot_filters_and_regrouping(app_env):
    at = run_app()
    units = sorted(u for u in at.multiselect(key="pv_f_unit").options)
    # filter to one unit, then to another: the table follows, no error on the re-render
    for u in units[:2]:
        at.multiselect(key="pv_f_unit").set_value([u]).run()
        assert not _errors(at), _errors(at)
        piv = [d for d in at.dataframe if getattr(d, "key", None) == "tbl_pivot"]
        assert piv and piv[0].value["unit_key"].tolist() == [u]
    # regroup by city with year filter and the yield measure
    at.multiselect(key="pv_rows").set_value(["city"]).run()
    at.multiselect(key="pv_f_year").set_value([at.multiselect(key="pv_f_year").options[0]]).run()
    at.selectbox(key="pv_val").set_value("yield_on_t0").run()
    assert not _errors(at), _errors(at)
    piv = [d for d in at.dataframe if getattr(d, "key", None) == "tbl_pivot"][0].value
    assert list(piv.columns)[0] == "city" and len(piv.columns) == 2  # one year, no TOTAL for yields
    at.selectbox(key="pv_cols").set_value("(none)").run()
    at.multiselect(key="pv_rows").set_value([]).run()
    assert not _errors(at), _errors(at)


def test_returns_tab_has_t0_values_and_both_models(app_env):
    at = run_app()
    live = [d for d in at.dataframe if getattr(d, "key", None) is None and "value_t0" in list(d.value.columns)]
    assert live, "unit values table missing"
    v = live[0].value
    flats = v[(v["unit_key"] != "TOTAL")]
    assert flats["value_t0"].notna().sum() == len(flats) - 1  # only the ledger-only unit lacks a T0
    year_cols = [c for c in v.columns if c.isdigit()]
    assert year_cols and v[year_cols].notna().any(axis=None)
    # the gap report names the ledger-only unit and nothing else
    gaps = [d for d in at.dataframe if "why" in list(d.value.columns)]
    assert gaps and len(gaps[0].value) == 1
    # the price table model still runs
    at.radio(key="val_mode").set_value("Paste a price/m² table").run()
    assert not _errors(at), _errors(at)
    prices = [t for t in at.text_area if t.label.startswith("Price per m")][0]
    prices.set_value("2024\t9000\n2025\t9500").run()
    assert [d for d in at.dataframe if getattr(d, "key", None) == "tbl_returns_total_return"]
    assert not _errors(at), _errors(at)
    at.radio(key="val_mode").set_value("Annual % change per city").run()
    at.number_input(key="growth_Krakow").set_value(7.5).run()
    assert not _errors(at), _errors(at)
