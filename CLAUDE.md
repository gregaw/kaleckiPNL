# Working conventions for kaleckiPNL

Read `README.md` first: what the app is, the sheet layout, the definitions table, the security
model. This file is what a change must respect.

## Non-negotiables

- **The real ledger is secret.** It is never committed, never uploaded, never pasted into a
  prompt or a log. Develop against `tests/data/sample.xlsx`, produced by `kalecki/testdata.py`.
  If you need to check a parser change against the real file, print shapes and counts only.
- **Loopback only.** `127.0.0.1` in `.streamlit/config.toml`, `run.sh` and the guard at the top
  of `app.py`. Never `0.0.0.0`.
- **Network egress is two calls.** NBP (currency code + date) and Yahoo (tickers), in
  `kalecki/market.py` behind `assert_allowed`; Drive in `sync.py`. `tests/test_security.py`
  fails if anything else imports a network library. Amounts, unit names and positions never
  leave the machine.
- **Secrets live in the Keychain**, via `kalecki/store.py::KeyStore`: OAuth client, Drive token,
  vault key. No `.env`, no token files.
- **Drive scope is `drive.readonly`** only.
- **The vault is AES-256-GCM** around parquet; nothing decrypted is written to disk.

## Where things live

| Path | What |
|---|---|
| `app.py` | Streamlit dashboard: loopback guard, sidebar (sync, upload, sign-in, market data, shutdown), eight tabs. `table(..., drill=pnl)` gives any aggregate table a click-a-cell drill-down. |
| `sync.py` | Drive OAuth + export, local-file ingest, sync log; CLI. |
| `engine.py` | DuckDB schema; rental engine (sheet formulas, FX, arrears, yearly summary, ryczałt, schedule); FIFO/LIFO/average trades. Pure, tested. |
| `kalecki/parse.py` | xlsx → frames; header found by content, columns by normalized prefix. Pure, tested. |
| `kalecki/analysis.py` | Valuations (per-city growth model, pasted price table), returns, portfolio returns, pivot/drill helpers, problems report. Pure, tested. |
| `kalecki/store.py` | Vault + KeyStore. Tested with `MemoryKeyStore`. |
| `kalecki/market.py` | NBP and Yahoo adapters, allowlist, offline mode. NBP walk-back tested with a fake session. |
| `kalecki/config.py` | `config.toml` → `Config`. Nothing secret. |
| `kalecki/testdata.py` | Synthetic workbook generator; plants one arrears unit, one vacancy, one gap, one formula mismatch. |

## Shipping a change

```bash
.venv/bin/python -m pytest -q                    # must be green; CI runs exactly this
.venv/bin/python -m kalecki.testdata tests/data/sample.xlsx --units 12 --months 18 --seed 7   # only if the generator changed
./run.sh                                         # then click every tab once
```

Keep new logic in `engine.py` / `kalecki/analysis.py` as pure functions on polars frames with a
test; keep `app.py` to layout and wiring. The sheet's formulas are reproduced in
`engine.monthly_pnl` — if the owner changes a formula in the sheet, change it there and the
`discrepancies` check will confirm the match on the next sync.

## Unverified from the build environment

NBP endpoint, Yahoo quotes, Drive consent, Keychain. Say so when touching them.
