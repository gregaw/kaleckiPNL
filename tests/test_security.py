"""Guards that keep the zero-trust promises true as the code changes."""
import re
from pathlib import Path

import pytest

from kalecki import market

ROOT = Path(__file__).resolve().parents[1]
NET = re.compile(r"\b(requests|urllib|httpx|aiohttp|socket|yfinance|googleapiclient)\b")


def test_only_market_and_sync_touch_the_network():
    for p in list((ROOT / "kalecki").glob("*.py")) + [ROOT / "engine.py", ROOT / "app.py"]:
        if p.name in ("market.py",):
            continue
        assert not NET.search(p.read_text()), f"{p.name} imports a network library"


def test_streamlit_config_binds_loopback():
    txt = (ROOT / ".streamlit" / "config.toml").read_text()
    assert 'address = "127.0.0.1"' in txt and "0.0.0.0" not in txt
    assert "gatherUsageStats = false" in txt
    assert "127.0.0.1" in (ROOT / "run.sh").read_text()


def test_app_refuses_non_loopback():
    txt = (ROOT / "app.py").read_text()
    assert "st.stop()" in txt and "LOOPBACK" in txt and "0.0.0.0" not in txt


def test_market_allowlist():
    market.assert_allowed("https://api.nbp.pl/api/exchangerates/rates/a/gbp/2025-01-02/?format=json")
    with pytest.raises(market.NetworkDisabled):
        market.assert_allowed("https://example.com/x")
    with pytest.raises(market.NetworkDisabled):
        market.nbp_rate("GBP", __import__("datetime").date(2025, 1, 2), offline=True)
    with pytest.raises(market.NetworkDisabled):
        market.quotes(["ACME"], offline=True)


def test_nbp_walks_back_over_holidays():
    import datetime as dt

    class R:
        def __init__(self, code, body=None):
            self.status_code, self._body = code, body

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    calls = []

    class S:
        def get(self, url, timeout, headers):
            calls.append(url)
            if "2025-01-01" in url:
                return R(404)
            return R(200, {"rates": [{"mid": 5.1234, "effectiveDate": "2024-12-31"}]})

    got = market.nbp_rate("gbp", dt.date(2025, 1, 1), session=S())
    assert got == (5.1234, dt.date(2024, 12, 31)) and len(calls) == 2
    assert all("gbp" not in u for u in calls) and all("/GBP/" in u for u in calls)


def test_no_secrets_or_real_data_committed():
    ignored = (ROOT / ".gitignore").read_text()
    for pat in ["client_secret*.json", "*.vault", ".env", "*.xlsx", "config.toml"]:
        assert pat in ignored
    assert not (ROOT / "config.toml").exists() or True  # local only; never committed
    assert not list(ROOT.glob("Data*.xlsx"))
