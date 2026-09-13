import os

import polars as pl
import pytest
from cryptography.exceptions import InvalidTag

import sync
from kalecki.config import Config, from_dict, spreadsheet_id
from kalecki.store import MAGIC, MemoryKeyStore, Vault, seal, unseal, vault_key


def test_vault_roundtrip_and_no_plaintext(tmp_path, parsed):
    ks = MemoryKeyStore()
    v = Vault(tmp_path / "k.vault", ks)
    v.save({"units": parsed.units, "rent_ledger": parsed.ledger})
    blob = (tmp_path / "k.vault").read_bytes()
    assert blob.startswith(MAGIC)
    assert b"Kowalski" not in blob and b"Krakow" not in blob and b"PAR1" not in blob  # no parquet magic either
    assert oct(os.stat(tmp_path / "k.vault").st_mode)[-3:] == "600"
    back = v.load()
    assert back["rent_ledger"].equals(parsed.ledger) and back["units"].equals(parsed.units)
    assert not list(tmp_path.glob(".vault-*"))  # no temp file left behind


def test_wrong_key_and_tamper_rejected(parsed):
    k1, k2 = os.urandom(32), os.urandom(32)
    blob = seal({"t": parsed.units}, k1)
    with pytest.raises(InvalidTag):
        unseal(blob, k2)
    bad = bytearray(blob)
    bad[-1] ^= 1
    with pytest.raises(InvalidTag):
        unseal(bytes(bad), k1)
    with pytest.raises(ValueError):
        unseal(b"nope" + blob, k1)


def test_key_created_once():
    ks = MemoryKeyStore()
    a = vault_key(ks)
    assert len(a) == 32 and vault_key(ks) == a


def test_local_sync_skips_unchanged(tmp_path):
    cfg = Config(vault=str(tmp_path / "v.vault"))
    ks = MemoryKeyStore()
    r1 = sync.sync(cfg, ks, "local:tests/data/sample.xlsx")
    assert r1.rows == 215 and not r1.skipped
    r2 = sync.sync(cfg, ks, "local:tests/data/sample.xlsx")
    assert r2.skipped
    r3 = sync.sync(cfg, ks, "local:tests/data/sample.xlsx", force=True)
    assert not r3.skipped
    t = Vault(cfg.vault, ks).load()
    assert t["sync_log"].height == 2 and set(t) >= {"units", "rent_ledger", "trades", "sync_log"}


def test_drive_scope_is_readonly_only():
    assert sync.SCOPES == ["https://www.googleapis.com/auth/drive.readonly"]


def test_oauth_client_validation():
    ks = MemoryKeyStore()
    with pytest.raises(ValueError):
        sync.store_oauth_client(ks, '{"web": {}}')
    sync.store_oauth_client(ks, '{"installed": {"client_id": "x"}}')
    assert sync.has_oauth_client(ks)


def test_spreadsheet_id_from_url():
    assert spreadsheet_id("https://docs.google.com/spreadsheets/d/1pgOLC1CYkyQq-blkMr1J9G2H9x9gWNOixVw3Yut5rM4/edit?gid=1#gid=1") == "1pgOLC1CYkyQq-blkMr1J9G2H9x9gWNOixVw3Yut5rM4"
    assert spreadsheet_id("1pgOLC1CYkyQq-blkMr1J9G2H9x9gWNOixVw3Yut5rM4") == "1pgOLC1CYkyQq-blkMr1J9G2H9x9gWNOixVw3Yut5rM4"
    with pytest.raises(ValueError):
        spreadsheet_id("nope")


def test_config_from_dict_defaults_and_overrides():
    cfg = from_dict({"tax": {"threshold_pln": 200000}, "analysis": {"muted": ["vacancy"]}, "fx": {"foreign_units": {"A": "gbp"}}})
    assert cfg.tax.threshold_pln == 200000.0 and cfg.tax.rate_low == 0.085
    assert cfg.analysis.muted == ["vacancy"] and cfg.foreign_units == {"A": "GBP"}
    assert cfg.ledger_tab == "All-Data"
