"""Degiro — valued from a manually-maintained holdings file × public prices.

The unofficial reverse-engineered Degiro connector is deliberately not used.
``data/degiro_holdings.csv`` is edited by hand when a buy or sell happens:

    isin,ticker,shares,cash_czk,note
    IE00BK5BQT80,VWCE.DE,42.5,0,VWCE in DIP wrapper

Prices come from Yahoo Finance (yfinance), with justETF by ISIN as a fallback.
When neither yields a fresh quote — weekend, holiday, delisted ticker — the last
known price is carried forward from ``data/price_cache.json`` and the row is
marked ``stale`` with the date the price actually comes from.

This figure will drift from Degiro's own number: fees, dividends and FX timing
are not modelled. That is expected and documented in the README.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
from decimal import Decimal
from pathlib import Path

import requests

from .base import STATUS_OK, STATUS_STALE, Row, SourceError, dec

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_PATH = Path(
    os.environ.get("DEGIRO_HOLDINGS", REPO_ROOT / "data" / "degiro_holdings.csv")
)
PRICE_CACHE_PATH = Path(
    os.environ.get("DEGIRO_PRICE_CACHE", REPO_ROOT / "data" / "price_cache.json")
)
NATIVE_CURRENCY = os.environ.get("DEGIRO_NATIVE_CURRENCY", "EUR").upper()
USE_JUSTETF = os.environ.get("DEGIRO_USE_JUSTETF", "true").lower() != "false"
TIMEOUT = 30


def fetch(fx, dry_run: bool = False) -> Row:
    holdings = _read_holdings()
    if not holdings:
        raise SourceError(f"no holdings in {HOLDINGS_PATH}")

    cache = _read_cache()
    today = dt.date.today().isoformat()

    positions_czk = Decimal(0)
    cash_czk = Decimal(0)
    stale_dates: list[str] = []
    unpriced: list[str] = []

    for holding in holdings:
        cash_czk += dec(holding["cash_czk"])
        shares = dec(holding["shares"])
        if shares == 0:
            continue

        quote = _price(holding, cache)
        if quote is None:
            unpriced.append(holding["ticker"] or holding["isin"])
            continue

        price, currency, priced_on = quote
        positions_czk += fx.to_czk(shares * price, currency)
        if priced_on != today:
            stale_dates.append(priced_on)

    if not dry_run:
        _write_cache(cache)

    if unpriced and positions_czk == 0:
        raise SourceError(f"no price for any holding ({', '.join(unpriced)})")

    total_czk = positions_czk + cash_czk
    rate = fx.rate(NATIVE_CURRENCY)

    row = Row(
        date="",
        source="degiro",
        currency=NATIVE_CURRENCY,
        cash=cash_czk / rate,
        positions_value=positions_czk / rate,
        status=STATUS_OK,
    )
    row.total_czk = total_czk

    notes = []
    if stale_dates:
        row.status = STATUS_STALE
        notes.append(f"priced {min(stale_dates)}")
    if unpriced:
        row.status = STATUS_STALE
        notes.append(f"unpriced: {', '.join(unpriced)}")
    row.note = "; ".join(notes)
    return row


def _read_holdings() -> list[dict]:
    if not HOLDINGS_PATH.exists():
        raise SourceError(f"{HOLDINGS_PATH} not found")
    with HOLDINGS_PATH.open(newline="", encoding="utf-8") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            if not raw or not (raw.get("isin") or raw.get("ticker")):
                continue
            rows.append(
                {
                    "isin": (raw.get("isin") or "").strip(),
                    "ticker": (raw.get("ticker") or "").strip(),
                    "shares": (raw.get("shares") or "0").strip(),
                    "cash_czk": (raw.get("cash_czk") or "0").strip(),
                    "note": (raw.get("note") or "").strip(),
                }
            )
        return rows


def _price(holding: dict, cache: dict) -> tuple[Decimal, str, str] | None:
    """(price, currency, pricing date). Falls back to the carried-forward price."""
    key = holding["ticker"] or holding["isin"]
    today = dt.date.today().isoformat()

    fresh = _yahoo(holding["ticker"]) if holding["ticker"] else None
    if fresh is None and USE_JUSTETF and holding["isin"]:
        fresh = _justetf(holding["isin"])

    if fresh is not None:
        price, currency = fresh
        cache[key] = {"price": str(price), "currency": currency, "date": today}
        return price, currency, today

    cached = cache.get(key)
    if cached:
        return dec(cached.get("price")), cached.get("currency", NATIVE_CURRENCY), cached.get("date", "")
    return None


def _yahoo(ticker: str) -> tuple[Decimal, str] | None:
    try:
        import yfinance
    except ImportError:
        return None
    try:
        info = yfinance.Ticker(ticker).fast_info
        price = dec(info.get("last_price") if isinstance(info, dict) else info.last_price)
        currency = (
            info.get("currency") if isinstance(info, dict) else info.currency
        ) or NATIVE_CURRENCY
    except Exception:
        return None
    if price <= 0:
        return None
    return price, currency.upper()


def _justetf(isin: str) -> tuple[Decimal, str] | None:
    """Fallback quote by ISIN. Deliberately forgiving — any failure returns None
    and the caller carries the cached price forward instead."""
    url = f"https://www.justetf.com/en/etf-profile.html?isin={isin}"
    try:
        resp = requests.get(
            url, timeout=TIMEOUT, headers={"User-Agent": "portfolio-tracker/1.0"}
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    match = re.search(
        r'"?(EUR|USD|GBP|CHF|CZK)"?\s*</?[^>]*>?\s*([0-9]+[.,][0-9]{2})', resp.text
    )
    if not match:
        return None
    currency, raw = match.group(1), match.group(2).replace(",", ".")
    price = dec(raw)
    return (price, currency) if price > 0 else None


def _read_cache() -> dict:
    if not PRICE_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(PRICE_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_cache(cache: dict) -> None:
    if not cache:
        return
    try:
        PRICE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PRICE_CACHE_PATH.write_text(
            json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
