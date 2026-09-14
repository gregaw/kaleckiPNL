# kaleckiPNL

Local-first, zero-trust PnL analytics for a rental-property ledger kept in a Google Sheet,
with a FIFO engine for securities on the side. Runs on your Mac, binds to `127.0.0.1` only,
stores everything encrypted, and sends nothing but public identifiers over the network.

## What it does

- **Sync** the ledger from Google Drive (read-only scope, PKCE desktop sign-in, token in the
  macOS Keychain) or load a local `.xlsx` export.
- **Store** the parsed tables in an AES-256-GCM vault (`data/kalecki.vault`); the key lives in
  the Keychain, never on disk. Analytics run in an in-memory DuckDB.
- **Reproduce the sheet's arithmetic** (expected transfer, balance, taxable profit) and flag
  every row where the sheet disagrees with itself.
- **Returns** per unit per year: give each city an annual % change in value and every unit is
  valued from its T0 price (values update live as you type, so the rate is easy to sanity-check);
  or paste a `year → PLN/m²` table. Income, capital and total return, before and after tax, with
  a portfolio row.
- **Pivot**: slice the monthly ledger by unit, year, month, city, manager, kind…; spread one
  dimension across the columns; sum/mean/min/max/count of any measure or yield on T0. A bar chart
  follows the table.
- **Drill down**: click a cell in any aggregate table (pivot, returns, yearly summary, arrears,
  tax base) or a bar in the yield / pivot charts to see the monthly ledger rows behind it.
- **Problems**: arrears, underpayment by manager, vacancies, missing/duplicate months, units
  missing from a tab, formula mismatches, media advances below actual bills, rents not indexed
  for a year, missing FX rates.
- **Tax & ops**: ryczałt 8.5 % / 12.5 % on the yearly base, the monthly advance schedule with
  due dates, expected cash next month per manager.
- **Trades** (optional `Trades` tab): FIFO / LIFO / average cost basis, realised and unrealised
  PnL, quotes from Yahoo by ticker only.

## Install

```bash
git clone <this repo> && cd kaleckiPNL
cp config.example.toml config.toml        # then edit: spreadsheet URL, thresholds, valuations
./run.sh                                  # creates .venv, installs, starts on http://127.0.0.1:8501
```

Try it without Google first:

```bash
.venv/bin/python -m kalecki.testdata sample.xlsx --units 12 --months 18
.venv/bin/python sync.py --source local:sample.xlsx
./run.sh
```

## Google Drive, once

1. In Google Cloud, create an **OAuth client of type Desktop app** with the Drive API enabled and
   `https://www.googleapis.com/auth/drive.readonly` on the consent screen. Download the JSON.
2. `python sync.py --client-secret ~/Downloads/client_secret_….json` stores it in the Keychain.
   Delete the file afterwards. (Or paste it under *Google sign-in* in the sidebar.)
3. Put the sheet's URL in `config.toml` under `[source] spreadsheet`.
4. `python sync.py` or **Sync now** opens the browser for consent; the redirect lands on a
   loopback port. The refresh token is stored in the Keychain as `drive_token`.

The sheet is exported through `files.export` (10 MB limit) — the app never gets edit access.

## Sheet layout expected

Tab `All-Data` (name configurable): header row anywhere, columns matched by name prefix —
`Lokal, Rok, Zarzad, Miasto, Adres, nr, wl, mc, Czynsz…, Zaliczka na media…, Wpłata Najemcy,
Faktura za zarządzanie, Opłata do Wspólnoty, Faktura PGE, Naprawy…, Przelew, Przelew spodziewany,
nadplata…, Koszty poza podatkiem…, Zysk-do-podatku, data kursu, kurs NBP`.

Tab `Reference`: `<label>, mieszkanie, typ, wartosc T0-najem, powierzchnia, pietro, KW, T0`.

Optional tab `Trades`: `Date, Ticker, Side, Qty, Price, Fee, Currency, Account`.

A unit with a `kurs NBP` value is treated as GBP; override per unit under `[fx] foreign_units`.

## Definitions

| Quantity | Formula |
|---|---|
| expected transfer | contract rent − management invoice − repairs − non-tax costs |
| balance | transfer − expected transfer (negative = short) |
| taxable profit (ryczałt base) | transfer + repairs + management invoice |
| media result | media advance − HOA − electricity (when the owner pays those; `[analysis]`) |
| net income | taxable profit − non-tax costs + media result |
| unit value (growth model) | T0 value × (1 + city % change)^(year − T0 year); garages follow their city |
| unit value (price table) | price/m² × area for flats; garage = T0 value × city price index, or a `garaz` column |
| income return | annualised net income ÷ value at the start of the year |
| capital return | (value − previous value) ÷ previous value |
| after tax | the year's ryczałt allocated to units pro rata to taxable profit |

## Security model

- Server bound to `127.0.0.1:8501` by `.streamlit/config.toml`, `run.sh`, and a guard in
  `app.py` that stops on any other address.
- Secrets only in the Keychain: OAuth client, Drive token, vault key. No `.env`.
- Network egress limited to `api.nbp.pl` (currency code + date) and Yahoo (tickers), enforced by
  an allowlist in `kalecki/market.py`; a test fails if any other module imports a network
  library. The **Offline** toggle disables both.
- `.gitignore` excludes `config.toml`, `*.xlsx`, `*.vault`, `client_secret*.json`.

## Development

```bash
.venv/bin/python -m pytest -q       # 46 tests on the synthetic fixture; no network
```

Not verified from the build environment (no access there): the NBP endpoint, Yahoo quotes,
the Drive consent flow and Keychain writes. Each degrades with a message rather than failing
the dashboard.
