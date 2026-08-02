# Portfolio balance tracker

One command pulls every portfolio balance, appends it to a local CSV, and
rebuilds a self-contained dashboard you can double-click.

```bash
python snapshot.py --open
```

No database, no server, no cloud. **Your balances never leave this machine** —
the CSV, the price cache and your Degiro holdings are all gitignored. This repo
is code only.

```
snapshot.py                      entrypoint — fetch, write CSV, build dashboard
dashboard_template.html          the dashboard, with a slot for the data
dashboard.html                   generated on each run (gitignored)
sources/
  base.py                        shared Row type and helpers
  t212.py                        Trading 212 official API
  coinbase.py                    Coinbase Advanced Trade SDK
  coinmate.py                    Coinmate REST
  degiro.py                      holdings CSV x public price lookup
  fx.py                          ČNB daily rates
data/                            all gitignored except the example
  balances.csv                   your history — the only copy, back it up
  degiro_holdings.csv            edited by hand when you buy or sell
  degiro_holdings.example.csv    committed template
  price_cache.json               last known price per ticker (carry-forward)
  index_base.json                index-mode base, only in index mode
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env               # fill in your keys
cp data/degiro_holdings.example.csv data/degiro_holdings.csv
python snapshot.py --open
```

Keys live in `.env` only. Even though nothing sensitive is committed, **scope
every key read-only / view-only** — no withdrawal or trade rights on the crypto
keys. A read-only key that leaks is an annoyance; a withdrawal-scoped one is an
irreversible loss.

## Usage

```bash
python snapshot.py                 # refresh the CSV and dashboard.html
python snapshot.py --open          # ...and open it in your browser
python snapshot.py --dry-run       # print the rows, touch nothing on disk
python snapshot.py --only t212     # one source (repeatable or comma-separated)
python snapshot.py --no-dashboard  # update the CSV only
DEBUG=true python snapshot.py      # tracebacks on unexpected fetcher errors
```

Run it whenever you want a fresh point. Rows are keyed on `(date, source)`, so
running it five times in one day leaves one row per source, not five — the last
run of the day wins.

If you later want it automatic, any scheduler works: `cron`, Task Scheduler, or
a systemd timer calling `python /path/to/snapshot.py`. Nothing in the script
assumes a schedule.

## Output — `data/balances.csv`

```csv
date,source,currency,cash,positions_value,total_native,total_czk,status,note
2026-08-01,t212,EUR,120.50,18240.00,18360.50,451234.12,ok,
2026-08-01,coinbase,EUR,0.00,4210.00,4210.00,103512.40,ok,
2026-08-01,coinmate,CZK,1500.00,62000.00,63500.00,63500.00,ok,
2026-08-01,degiro,EUR,0.00,31200.00,31200.00,767000.00,stale,priced 2026-07-31
```

- `date` — UTC date of the run.
- `status` — `ok` | `stale` | `error`. **A failing source never drops its row.**
  It gets an `error` row with the reason in `note`, so an outage is a visible gap
  rather than a silently absent day.
- On an `error` row every amount column is **empty, not `0.00`** — a zero would
  let the chart draw a line down to the floor, which is exactly the
  smoothing-over the status column exists to prevent.
- Numbers are plain decimals: dot separator, no thousands separators, no currency
  symbols — so Excel and the dashboard both read it directly.
- One source failing never aborts the run. The exit code is non-zero only if
  *every* source failed.

## Sources

| Source | How it is valued |
|---|---|
| **Trading 212** | Official public API (beta). HTTP Basic — key as username, secret as password. `GET /equity/account/cash` for cash, `GET /equity/portfolio` summed for positions. |
| **Coinbase** | Official `coinbase-advanced-py` SDK, which signs the JWT internally. `get_accounts()` for balances, valued from Coinbase's own product prices. |
| **Coinmate** | Official REST. `POST /api/balances`, HMAC-SHA256 signature. Crypto valued from the public ticker. |
| **Degiro** | **No API.** A manually-maintained holdings file × public market prices. |

### Trading 212

Works for **Invest / Stocks ISA accounts only**. Responses come back in the
account's primary currency only — multi-currency accounts are not supported, so
the figures are treated as already single-currency and no per-position FX is
attempted. Rate limits are enforced **per account**, not per key or IP, so
retrying tightly does not help; the client reads `x-ratelimit-remaining` and
waits.

### Coinbase

Needs a CDP API key from the Coinbase Developer Portal. **Ed25519 is the
recommended key type**; the SDK auto-detects it. Grant *view* permission only.
Reported in `COINBASE_QUOTE_CURRENCY` (EUR by default) and converted to CZK once.

### Coinmate

The signature is the part that goes wrong. HMAC-SHA256 over
`nonce + clientId + publicKey`, keyed with the private key, hex, **uppercased**;
`nonce` is epoch milliseconds and must strictly increase. Wrong order or wrong
casing returns a generic `Invalid request`. Verified against
[coinmate-io/coinmate-api-examples](https://github.com/coinmate-io/coinmate-api-examples).

### Degiro

The unofficial reverse-engineered Degiro connector is deliberately **not** used.
Edit `data/degiro_holdings.csv` when a buy or sell happens:

```csv
isin,ticker,shares,cash_czk,note
IE00BK5BQT80,VWCE.DE,42.5,0,VWCE in DIP wrapper
```

Prices come from Yahoo Finance (`yfinance`) by ticker, with justETF by ISIN as a
fallback. When neither returns a fresh quote — weekend, holiday, delisted — the
last known price is carried forward from `data/price_cache.json` and the row is
marked `stale` with the date the price actually comes from.

> **This figure will drift from Degiro's official number.** Fees, dividends and
> FX timing are not modelled. That is expected. Use the Degiro app for the
> authoritative number; use this for the shape of the curve.

**Prices update themselves; share counts do not.** If you buy or sell and forget
to edit this file, the tracker keeps valuing your old position at today's price
and still reports `ok`. Edit it in the same sitting as the trade.

The one change that could go wrong without you doing anything is a **share
split**: the price halves, your share count doesn't, and the value silently
halves with it. So a one-day price move of 35% or more marks the row `stale`
with a note naming the ticker and the move — and if yfinance knows about a
recent split, the note names that too:

```
VWCE.DE price moved -50.0% on 2026-06-01 — VWCE.DE split 2:1 on 2026-06-01;
verify shares in degiro_holdings.csv
```

The flag is **sticky**: it survives across runs, and across days when the price
source is down, until you change that holding's share count — the edit it was
asking for is what clears it. A warning you can scroll past once is no warning.

Tune or disable with `DEGIRO_PRICE_JUMP_PCT` (default `0.35`, `0` disables). Raise
it if you hold something genuinely volatile enough to move 35% in a day.

`cash_czk` is a CZK figure, but the row reports in `DEGIRO_NATIVE_CURRENCY`
(EUR by default) so `cash + positions_value = total_native` stays true in one
currency. Set `DEGIRO_NATIVE_CURRENCY=CZK` for a literal pass-through.

### FX

ČNB "denní kurz" daily feed, parsed from the pipe-delimited
`Country|Currency|Amount|Code|Rate` lines. The `Amount` column matters — some
currencies are quoted per 100 units. ČNB does not publish on weekends or Czech
public holidays; requesting the feed without a date already returns the most
recent published day, so the fallback is free. Rates are fetched **once per
run**, so four sources cost one HTTP request.

If the feed is unreachable the run does not abort: CZK-native sources still
report and the rest write `error` rows.

## Dashboard

`snapshot.py` bakes the CSV into `dashboard.html` from
`dashboard_template.html`. Embedding rather than fetching is deliberate: a page
opened via `file://` is not allowed to read a sibling file, so a fetch-based
dashboard would need a web server just to show anything. Double-click works.

- Total portfolio value in CZK over time, stacked breakdown by source, latest
  snapshot per source with status.
- **A day where any source failed has no total.** The line breaks and the day is
  marked on the baseline, because summing only the sources that did report draws
  a cliff that looks like a real loss.
- `stale` rows are flagged with the date the price came from.
- Table view gives every value without hovering; the palette is validated for
  colour-vision deficiency in both light and dark mode.

To edit the dashboard, change `dashboard_template.html` and re-run — never edit
`dashboard.html`, it is overwritten.

## Index mode

Set `INDEX_MODE=true` in `.env` to store shape only, no amounts:

- per-source rows: `total_czk` holds that source's **percentage share** of the day;
- one `_total` row per day: `total_czk` holds the **index**, 100 at the base date;
- `cash`, `positions_value`, `total_native` and `currency` are blank.

The index needs one absolute anchor: `INDEX_BASE_CZK` if set, otherwise seeded
into `data/index_base.json` on the first index-mode run. Mostly redundant now
that nothing is published — it exists for sharing a screenshot or handing the
CSV to someone.

> Switching modes rewrites the CSV in place. The absolute values are gone from
> the file, but restore an old copy and they are back.

## Excel

Do **not** write into the existing `.xlsx` from Python — `openpyxl` will
eventually mangle the formatting and formulas in the Dashboard/Tracker workbook.
Read the CSV instead:

1. **Data → Get Data → From Text/CSV**, point it at `data/balances.csv`.
2. **Load To… → Only Create Connection**, then add it to the sheet you want.
3. Right-click the query → **Properties** → tick **Refresh data when opening the
   file**.

Existing sheets stay untouched and read from the loaded table.

## Back it up

`data/balances.csv` is the only copy of your history and it is gitignored on
purpose. If this machine dies, the history dies. Copy it somewhere periodically,
or push it to a **private** repo of its own.

## Adding a source

1. Add `sources/yourthing.py` exposing `fetch(fx, dry_run=False) -> Row`.
   Raise `SourceError` with a human-readable reason on any failure.
2. Register it in `FETCHERS` in `snapshot.py` (order is also display order).
3. Add it to `SERIES` in `dashboard_template.html` with the next colour slot.

Slots past four need a re-validated palette — the current four are checked for
colour-vision deficiency in both modes.
