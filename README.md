# Portfolio balance tracker

Snapshots every portfolio balance once a day, appends the result to a CSV
committed back to this repo, and renders a static dashboard on GitHub Pages.
The same script runs locally unchanged.

No database, no external storage, no server. **The committed CSV is the history.**

```
.github/workflows/snapshot.yml   cron + manual trigger
snapshot.py                      entrypoint, runs all fetchers
sources/
  base.py                        shared Row type and helpers
  t212.py                        Trading 212 official API
  coinbase.py                    Coinbase Advanced Trade SDK
  coinmate.py                    Coinmate REST
  degiro.py                      holdings CSV x public price lookup
  fx.py                          ČNB daily rates
data/
  balances.csv                   append-only output, committed by the workflow
  degiro_holdings.csv            edited by hand, only when a buy or sell happens
  price_cache.json               last known price per ticker (carry-forward)
  index_base.json                index-mode base; only written in index mode
docs/
  index.html                     GitHub Pages dashboard
  balances.csv                   mirror of data/balances.csv, written by the workflow
```

## ⚠️ This repo is public

The balances CSV is world-readable. That is a deliberate, accepted trade — public
repos get unlimited Actions minutes and Pages on the free tier.

Two consequences you must not skip:

1. **Scope every API key read-only / view-only.** Do not grant withdrawal or
   trade permissions on the crypto keys. This is not a style preference: keys
   that reach a public repo get scraped within minutes, and a leaked
   withdrawal-scoped key is an immediate and irreversible loss.
2. **Never inline a credential.** `${{ secrets.NAME }}` is the same amount of
   typing as a literal.

If the absolute numbers become unwanted later, see [Index mode](#index-mode).

## Output — `data/balances.csv`

One row per source per day, upserted on `(date, source)`.

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
  in the chart rather than a silently absent day.
- On an `error` row every amount column is **empty, not `0.00`** — a zero would
  let the chart draw a line down to the floor, which is exactly the
  smoothing-over the status column exists to prevent.
- Numbers are plain decimals: dot separator, no thousands separators, no currency
  symbols. Excel Power Query and the dashboard both consume this directly.
- Notes are flattened to one line and commas become semicolons.

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
attempted.

Rate limits are enforced **per account**, not per key or per IP, so retrying
tightly does not help. The client reads `x-ratelimit-remaining` and waits.

### Coinbase

Needs a CDP API key from the Coinbase Developer Portal. **Ed25519 is the
recommended key type**; the SDK auto-detects the key type and picks the signing
algorithm. Grant *view* permission only.

Reported in `COINBASE_QUOTE_CURRENCY` (EUR by default) and converted to CZK once.

### Coinmate

The signature is the part that goes wrong. It is HMAC-SHA256 over
`nonce + clientId + publicKey`, keyed with the private key, hex, **uppercased**;
`nonce` is epoch milliseconds and must strictly increase. Get the order or the
casing wrong and the API returns a generic `Invalid request`. Verified against
[coinmate-io/coinmate-api-examples](https://github.com/coinmate-io/coinmate-api-examples).

### Degiro

The unofficial reverse-engineered Degiro connector is deliberately **not** used.
Instead, edit `data/degiro_holdings.csv` when a buy or sell happens:

```csv
isin,ticker,shares,cash_czk,note
IE00BK5BQT80,VWCE.DE,42.5,0,VWCE in DIP wrapper
```

Prices come from Yahoo Finance (`yfinance`) by ticker, with justETF by ISIN as a
fallback. When neither returns a fresh quote — weekend, holiday, delisted — the
last known price is carried forward from `data/price_cache.json` and the row is
marked `stale` with the date the price actually comes from.

> **This figure will drift from Degiro's official number.** Fees, dividends and
> FX timing are not modelled. That is expected and accepted. Use the Degiro app
> for the authoritative number; use this for the shape of the curve.

`cash_czk` is a CZK figure, but the row reports in `DEGIRO_NATIVE_CURRENCY`
(EUR by default) so that `cash + positions_value = total_native` stays true in a
single currency. It is converted at the ČNB rate on the way in; `total_czk` is
unaffected. Set `DEGIRO_NATIVE_CURRENCY=CZK` if you would rather the cash pass
through untouched.

### FX

ČNB "denní kurz" daily feed, parsed from the pipe-delimited
`Country|Currency|Amount|Code|Rate` lines. The `Amount` column matters — some
currencies are quoted per 100 units.

ČNB does not publish on weekends or Czech public holidays. Requesting the feed
without a date already returns the most recently published day, so the fallback
is free; an explicit walk-back covers the dated-request case. Rates are fetched
**once per run** and cached, so four sources still cost one HTTP request.

If the ČNB feed is unreachable the run does not abort: CZK-native sources still
report, and the sources that need a conversion write `error` rows.

## The workflow

Runs at 21:00 UTC (23:00 CEST, after the US close) and on demand.

- **`workflow_dispatch` is mandatory** — needed for testing and for forcing a
  snapshot right after a Degiro buy. It is the "Run workflow" button on the
  Actions tab.
- **Commits only if the CSV changed** (`git diff --cached --quiet`), so reruns
  do not produce empty commits.
- **Idempotent per day.** A row for `(date, source)` is overwritten, never
  duplicated. Running it twice on the same day leaves four rows, not eight.
- **One source failing does not fail the run.** Each fetcher is wrapped; the run
  exits non-zero only if *every* source failed.
- Actions cron is best-effort and can fire 5–30 minutes late under load. Nothing
  here depends on exact timing.
- Scheduled workflows are auto-disabled after 60 days of repository inactivity.
  The daily commit keeps this repo active, so it is a non-issue in practice —
  but if you ever pause the schedule for two months, re-enable it by hand.

### Secrets

Set these as repository secrets (Settings → Secrets and variables → Actions):

```
T212_API_KEY              T212_API_SECRET
COINBASE_API_KEY_NAME     COINBASE_API_PRIVATE_KEY
COINMATE_CLIENT_ID        COINMATE_PUBLIC_KEY        COINMATE_PRIVATE_KEY
```

The same names go in `.env` for local runs. `.env` is gitignored; `.env.example`
ships with empty values.

## Dashboard

`docs/index.html` is a single static file with no build step and no CDN — it
fetches the CSV at runtime and renders everything in inline SVG.

- Total portfolio value in CZK over time (line).
- Stacked breakdown by source.
- Latest snapshot per source with its status.
- `stale` and `error` rows are **flagged, not smoothed over**: a failed day
  breaks the line and drops a red marker on the baseline instead of plotting a
  zero, and a banner names the sources that did not report.
- A table view gives every value without hovering, and the palette is validated
  for colour-vision deficiency in both light and dark mode.

### Enabling Pages

Settings → Pages → Deploy from a branch → your default branch, folder **`/docs`**.

Pages treats `/docs` as the site root, so the page cannot reach `../data/`. The
workflow therefore mirrors `data/balances.csv` to `docs/balances.csv`, and the
page tries `./balances.csv`, `../data/balances.csv` and `./data/balances.csv` in
turn — so it also works when served from the repo root locally:

```bash
python3 -m http.server -d . 8000   # then open localhost:8000/docs/
```

## Index mode

If absolute values in a public repo become unwanted, set the repository variable
`INDEX_MODE=true` (or `INDEX_MODE=true` in `.env` locally). The CSV then carries
shape only, no amounts:

- per-source rows: `total_czk` holds that source's **percentage share** of the day;
- one extra `_total` row per day: `total_czk` holds the **portfolio index**,
  100 at the base date;
- `cash`, `positions_value`, `total_native` and `currency` are blank.

The index needs one absolute anchor. It is taken from `INDEX_BASE_CZK` if set —
put it in a repo **secret** to publish nothing absolute at all — otherwise it is
seeded into `data/index_base.json` on the first index-mode run, which does commit
that single number.

> Switching modes rewrites the whole CSV, but **git history still holds the old
> absolute rows**. If that matters, purge the history (or start a fresh repo) as
> well as flipping the flag.

The dashboard detects index mode from the `_total` rows and relabels itself.

## Excel

Do **not** write into the existing `.xlsx` from Python — `openpyxl` will
eventually mangle the formatting and formulas in the Dashboard/Tracker workbook.
Read the CSV instead:

1. **Data → Get Data → From Text/CSV**
2. Point it at the raw CSV URL:
   `https://raw.githubusercontent.com/<owner>/<repo>/main/data/balances.csv`
3. **Load To… → Only Create Connection**, then add it to the sheet you want.
4. Right-click the query → **Properties** → tick **Refresh data when opening the
   file**.

Existing sheets stay untouched and read from the loaded table.

## Local development

```bash
pip install -r requirements.txt
cp .env.example .env    # fill in
python snapshot.py --dry-run     # print the rows, write nothing
python snapshot.py --only t212   # one source (repeatable or comma-separated)
python snapshot.py               # real run, writes data/balances.csv
```

`--dry-run` touches nothing on disk — not the CSV, not the price cache — so
debugging one broken fetcher never needs a full run. Set `DEBUG=true` for
tracebacks on unexpected fetcher errors.

## Adding a source

1. Add `sources/yourthing.py` exposing `fetch(fx, dry_run=False) -> Row`.
   Raise `SourceError` with a human-readable reason on any failure.
2. Register it in `FETCHERS` in `snapshot.py` (the order is also the display
   order).
3. Add it to `SERIES` in `docs/index.html` with the next categorical colour slot.

Slots past four need a re-validated palette — see the note in `docs/index.html`.
