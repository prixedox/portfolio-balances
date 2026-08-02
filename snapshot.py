#!/usr/bin/env python3
"""Snapshot every portfolio source, append to data/balances.csv, rebuild the dashboard.

Everything runs on your machine: credentials come from .env, and neither the
balances nor the generated dashboard are ever committed.

    python snapshot.py                 # refresh the CSV and dashboard.html
    python snapshot.py --open          # ...and open the dashboard in a browser
    python snapshot.py --dry-run       # print the rows, touch nothing on disk
    python snapshot.py --only t212     # one source (repeatable, or comma-separated)
    python snapshot.py --no-dashboard  # update the CSV only

Exit code is 0 as long as at least one source produced a balance. A single
broken source writes an ``error`` row and the run carries on, so an outage shows
up as a visible gap in the dashboard rather than a missing day.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import traceback
from decimal import Decimal
from pathlib import Path

from sources import coinbase, coinmate, degiro, fx, t212
from sources.base import (
    COLUMNS,
    STATUS_ERROR,
    Row,
    SourceError,
    dec,
    money,
)

REPO_ROOT = Path(__file__).resolve().parent
CSV_PATH = Path(os.environ.get("BALANCES_CSV", REPO_ROOT / "data" / "balances.csv"))
INDEX_BASE_PATH = REPO_ROOT / "data" / "index_base.json"
TEMPLATE_PATH = REPO_ROOT / "dashboard_template.html"
DASHBOARD_PATH = Path(os.environ.get("DASHBOARD_HTML", REPO_ROOT / "dashboard.html"))
CSV_PLACEHOLDER = "__BALANCES_CSV__"

# Fetch order is also the display order in the dashboard.
FETCHERS = [
    ("t212", t212.fetch),
    ("coinbase", coinbase.fetch),
    ("coinmate", coinmate.fetch),
    ("degiro", degiro.fetch),
]

TOTAL_ROW_SOURCE = "_total"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _load_dotenv()

    selected = _select(args.only)
    run_date = dt.datetime.now(dt.timezone.utc).date().isoformat()
    index_mode = _env_flag("INDEX_MODE")

    rates = _get_rates()

    rows: list[Row] = []
    for name, fetcher in selected:
        rows.append(_run_one(name, fetcher, rates, run_date, args.dry_run))

    _print(rows, rates)

    succeeded = [row for row in rows if row.status != STATUS_ERROR]
    if not succeeded:
        print("\nAll sources failed.", file=sys.stderr)

    if args.dry_run:
        print("\n--dry-run: balances.csv and dashboard.html not written.")
        return 0 if succeeded else 1

    existing = _read_csv()
    merged = _merge(existing, rows)
    if index_mode:
        merged = _to_index_mode(merged)
    _write_csv(merged)
    print(f"\nWrote {len(merged)} rows to {CSV_PATH}")

    if not args.no_dashboard:
        built = _build_dashboard()
        if built:
            print(f"Dashboard   {DASHBOARD_PATH}")
            if args.open:
                _open_dashboard()

    return 0 if succeeded else 1


# --------------------------------------------------------------------------
# fetching


def _run_one(name, fetcher, rates, run_date: str, dry_run: bool) -> Row:
    """Never raises. A failed fetcher becomes a visible ``error`` row."""
    try:
        row = fetcher(rates, dry_run=dry_run)
        row.date = run_date
        row.source = name
        return row
    except SourceError as exc:
        return _error_row(name, run_date, str(exc))
    except Exception as exc:  # a fetcher bug must not take down the run
        if _env_flag("DEBUG"):
            traceback.print_exc()
        return _error_row(name, run_date, f"{type(exc).__name__}: {exc}")


def _error_row(name: str, run_date: str, reason: str) -> Row:
    return Row(
        date=run_date,
        source=name,
        status=STATUS_ERROR,
        note=_clean_note(reason),
    )


def _clean_note(text: str) -> str:
    """Notes live in a CSV cell — keep them one line and comma-free."""
    return " ".join(str(text).split()).replace(",", ";")[:200]


def _get_rates():
    try:
        return fx.get_rates()
    except SourceError as exc:
        print(f"warning: {exc}", file=sys.stderr)
        return _FailedFx(str(exc))


class _FailedFx:
    """Stands in when the ČNB feed is unreachable so that CZK-native sources
    (which never need a conversion) can still report."""

    rates_date = ""

    def __init__(self, reason: str):
        self.reason = reason

    def to_czk(self, amount, currency: str) -> Decimal:
        if (currency or "CZK").upper() == "CZK":
            return dec(amount)
        raise SourceError(self.reason)

    def rate(self, currency: str) -> Decimal:
        if (currency or "CZK").upper() == "CZK":
            return Decimal(1)
        raise SourceError(self.reason)


# --------------------------------------------------------------------------
# CSV


def _read_csv() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row.get("date")]


def _merge(existing: list[dict], fresh: list[Row]) -> list[dict]:
    """Upsert on (date, source) so a manual rerun overwrites instead of duplicating."""
    by_key = {(row["date"], row["source"]): row for row in existing}
    for row in fresh:
        by_key[(row.date, row.source)] = row.to_csv_dict()

    order = {name: index for index, (name, _) in enumerate(FETCHERS)}
    return sorted(
        by_key.values(),
        key=lambda row: (row["date"], order.get(row["source"], 99), row["source"]),
    )


def _write_csv(rows: list[dict]) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CSV_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})
    tmp.replace(CSV_PATH)


# --------------------------------------------------------------------------
# dashboard


def _build_dashboard() -> bool:
    """Bake the CSV into a standalone dashboard.html.

    Embedding rather than fetching is what makes the file openable straight from
    disk: a page on file:// is not allowed to fetch a sibling file, so a
    fetch-based dashboard would need a web server running to show anything.
    """
    if not TEMPLATE_PATH.exists():
        print(f"warning: {TEMPLATE_PATH.name} missing, dashboard not built", file=sys.stderr)
        return False
    try:
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        csv_text = CSV_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not build dashboard: {exc}", file=sys.stderr)
        return False

    slots = template.count(CSV_PLACEHOLDER)
    if slots != 1:
        print(
            f"warning: {TEMPLATE_PATH.name} has {slots} {CSV_PLACEHOLDER} slots, expected 1"
            " — dashboard not built",
            file=sys.stderr,
        )
        return False

    # The CSV sits inside a <script> block, so any "</" in a note would end the
    # element early. Nothing else needs escaping — the block is not HTML-parsed.
    safe = csv_text.replace("</", "<\\/")
    try:
        DASHBOARD_PATH.write_text(
            template.replace(CSV_PLACEHOLDER, safe), encoding="utf-8"
        )
    except OSError as exc:
        print(f"warning: could not write {DASHBOARD_PATH}: {exc}", file=sys.stderr)
        return False
    return True


def _open_dashboard() -> None:
    import webbrowser

    try:
        webbrowser.open(DASHBOARD_PATH.resolve().as_uri())
    except Exception as exc:
        print(f"warning: could not open a browser: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# index mode


def _to_index_mode(rows: list[dict]) -> list[dict]:
    """Replace absolute amounts with shape-only figures.

    Per-source rows carry that source's percentage share of the day's portfolio
    in ``total_czk``; one extra ``_total`` row per day carries the portfolio
    index, 100 at the base date. Cash, positions and native totals are dropped.

    The base is one absolute number. It comes from ``INDEX_BASE_CZK`` if set
    (keep it in a repo secret to publish nothing absolute at all), otherwise
    from ``data/index_base.json``, which is seeded on the first index-mode run.
    """
    by_date: dict[str, list[dict]] = {}
    for row in rows:
        if row["source"] == TOTAL_ROW_SOURCE:
            continue
        by_date.setdefault(row["date"], []).append(row)

    if not by_date:
        return []

    base_date, base_total = _index_base(by_date)

    out: list[dict] = []
    for date in sorted(by_date):
        day = by_date[date]
        day_total = sum(dec(row.get("total_czk")) for row in day)

        for row in day:
            share = (
                dec(row.get("total_czk")) / day_total * 100 if day_total else Decimal(0)
            )
            out.append(
                {
                    **row,
                    "currency": "",
                    "cash": "",
                    "positions_value": "",
                    "total_native": "",
                    "total_czk": money(share) if row["status"] != STATUS_ERROR else "",
                }
            )

        index = day_total / base_total * 100 if base_total else Decimal(0)
        out.append(
            {
                "date": date,
                "source": TOTAL_ROW_SOURCE,
                "currency": "",
                "cash": "",
                "positions_value": "",
                "total_native": "",
                "total_czk": money(index),
                "status": "ok",
                "note": f"index base 100 @ {base_date}",
            }
        )
    return out


def _index_base(by_date: dict[str, list[dict]]) -> tuple[str, Decimal]:
    from_env = os.environ.get("INDEX_BASE_CZK")
    if from_env:
        return os.environ.get("INDEX_BASE_DATE", min(by_date)), dec(from_env)

    if INDEX_BASE_PATH.exists():
        try:
            stored = json.loads(INDEX_BASE_PATH.read_text(encoding="utf-8"))
            return stored["date"], dec(stored["total_czk"])
        except (OSError, ValueError, KeyError):
            pass

    base_date = min(by_date)
    base_total = sum(dec(row.get("total_czk")) for row in by_date[base_date])
    INDEX_BASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_BASE_PATH.write_text(
        json.dumps({"date": base_date, "total_czk": money(base_total)}, indent=2) + "\n",
        encoding="utf-8",
    )
    return base_date, base_total


# --------------------------------------------------------------------------
# cli plumbing


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the rows without writing data/balances.csv",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SOURCE",
        help="fetch one source only; repeatable or comma-separated",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="open the refreshed dashboard in your browser when done",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="update the CSV without rebuilding dashboard.html",
    )
    return parser.parse_args(argv)


def _select(only: list[str]):
    if not only:
        return FETCHERS
    wanted = {
        name.strip().lower()
        for item in only
        for name in item.split(",")
        if name.strip()
    }
    unknown = wanted - {name for name, _ in FETCHERS}
    if unknown:
        raise SystemExit(
            f"unknown source(s): {', '.join(sorted(unknown))}. "
            f"Known: {', '.join(name for name, _ in FETCHERS)}"
        )
    return [item for item in FETCHERS if item[0] in wanted]


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / ".env")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _print(rows: list[Row], rates) -> None:
    if getattr(rates, "rates_date", ""):
        print(f"ČNB rates: {rates.rates_date}")
    header = f"{'source':<10} {'cur':<4} {'cash':>14} {'positions':>16} {'total CZK':>16}  status"
    print(header)
    print("-" * len(header))
    total = Decimal(0)
    for row in rows:
        cells = row.to_csv_dict()
        print(
            f"{row.source:<10} {row.currency:<4} {cells['cash']:>14} "
            f"{cells['positions_value']:>16} {cells['total_czk']:>16}  "
            f"{row.status}{(' — ' + row.note) if row.note else ''}"
        )
        if row.status != STATUS_ERROR:
            total += row.total_czk
    print("-" * len(header))
    print(f"{'TOTAL':<10} {'CZK':<4} {'':>14} {'':>16} {money(total):>16}")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
