"""Non-secret configuration from config.toml. Secrets never live here — they are in the
system keychain (see store.py and sync.py)."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SPREADSHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


def spreadsheet_id(value: str) -> str:
    """Accept a full Google Sheets URL or a bare id; return the id."""
    value = (value or "").strip()
    m = SPREADSHEET_ID_RE.search(value)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    raise ValueError("source.spreadsheet must be a Google Sheets URL or a spreadsheet id")


@dataclass
class TaxConfig:
    threshold_pln: float = 100_000.0
    rate_low: float = 0.085
    rate_high: float = 0.125
    advance_due_day: int = 20


@dataclass
class AnalysisConfig:
    arrears_threshold: float = 500.0
    arrears_months: int = 2
    rent_unchanged_months: int = 12
    owner_pays_hoa: bool = True
    owner_pays_electricity: bool = True
    muted: list[str] = field(default_factory=list)


@dataclass
class Config:
    spreadsheet: str = ""
    ledger_tab: str = "All-Data"
    reference_tab: str = "Reference"
    trades_tab: str = "Trades"
    local_file: str = ""
    vault: str = "data/kalecki.vault"
    keyring_service: str = "kaleckiPNL"
    offline: bool = False
    tax: TaxConfig = field(default_factory=TaxConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    base_currency: str = "PLN"
    foreign_units: dict[str, str] = field(default_factory=dict)
    valuations_table: str = ""
    garage_value_index: bool = True
    growth: dict[str, float] = field(default_factory=dict)   # city -> annual % change, e.g. {"Krakow": 0.05}
    default_growth: float = 0.0

    @property
    def spreadsheet_id(self) -> str:
        return spreadsheet_id(self.spreadsheet)


def _pick(d: dict, cls, **rename):
    """Build a dataclass from a dict, keeping only known fields."""
    names = {f for f in cls.__dataclass_fields__}
    kwargs = {rename.get(k, k): v for k, v in d.items() if rename.get(k, k) in names}
    return cls(**kwargs)


def from_dict(raw: dict) -> Config:
    src = raw.get("source", {})
    sto = raw.get("storage", {})
    net = raw.get("network", {})
    fx = raw.get("fx", {})
    val = raw.get("valuations", {})
    cfg = Config(
        spreadsheet=str(src.get("spreadsheet", "")),
        ledger_tab=str(src.get("ledger_tab", "All-Data")),
        reference_tab=str(src.get("reference_tab", "Reference")),
        trades_tab=str(src.get("trades_tab", "Trades")),
        local_file=str(src.get("local_file", "")),
        vault=str(sto.get("vault", "data/kalecki.vault")),
        keyring_service=str(sto.get("keyring_service", "kaleckiPNL")),
        offline=bool(net.get("offline", False)),
        tax=_pick(raw.get("tax", {}), TaxConfig),
        analysis=_pick(raw.get("analysis", {}), AnalysisConfig),
        base_currency=str(fx.get("base_currency", "PLN")),
        foreign_units={str(k): str(v).upper() for k, v in fx.get("foreign_units", {}).items()},
        valuations_table=str(val.get("table", "")),
        garage_value_index=bool(val.get("garage_value_index", True)),
        growth={str(k): float(v) for k, v in val.get("growth", {}).items()},
        default_growth=float(val.get("default_growth", 0.0)),
    )
    cfg.tax.threshold_pln = float(cfg.tax.threshold_pln)
    return cfg


def load(path: str | Path = "config.toml") -> Config:
    p = Path(path)
    if not p.exists():
        return Config()
    with p.open("rb") as f:
        return from_dict(tomllib.load(f))
