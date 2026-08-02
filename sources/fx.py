"""ČNB daily exchange rates.

The feed is the "denní kurz" text file. Format:

    31 Jul 2026 #146
    Country|Currency|Amount|Code|Rate
    EMU|euro|1|EUR|24.210
    Japan|yen|100|JPY|15.887

Note the ``Amount`` column: some currencies are quoted per 100 or per 1000 units.

ČNB does not publish on weekends or Czech public holidays. Requesting the feed
without a ``date`` parameter already returns the most recently published day, so
the weekend fallback is free; the explicit walk-back below is a safety net for
the case where a dated request lands on a non-publishing day.

Rates are fetched once per process and cached, so a run with four sources still
performs a single HTTP request.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import requests

from .base import SourceError, dec

FEED_URL = (
    "https://www.cnb.cz/en/financial-markets/foreign-exchange-market/"
    "central-bank-exchange-rate-fixing/central-bank-exchange-rate-fixing/daily.txt"
)

TIMEOUT = 30


class FxRates:
    """Rates for one ČNB publication day, keyed by ISO currency code -> CZK."""

    def __init__(self, rates: dict[str, Decimal], rates_date: str):
        self.rates = dict(rates)
        self.rates["CZK"] = Decimal(1)
        self.rates_date = rates_date

    def to_czk(self, amount, currency: str) -> Decimal:
        amount = dec(amount)
        code = (currency or "CZK").upper()
        if code == "CZK":
            return amount
        rate = self.rates.get(code)
        if rate is None:
            raise SourceError(f"no ČNB rate for {code} on {self.rates_date}")
        return amount * rate

    def rate(self, currency: str) -> Decimal:
        code = (currency or "CZK").upper()
        if code == "CZK":
            return Decimal(1)
        rate = self.rates.get(code)
        if rate is None:
            raise SourceError(f"no ČNB rate for {code} on {self.rates_date}")
        return rate


_cache: FxRates | None = None


def get_rates(force: bool = False) -> FxRates:
    """Fetch and cache the current ČNB rates."""
    global _cache
    if _cache is not None and not force:
        return _cache
    _cache = _fetch(None)
    return _cache


def _fetch(day: dt.date | None) -> FxRates:
    params = {"date": day.strftime("%d.%m.%Y")} if day else None
    try:
        resp = requests.get(FEED_URL, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SourceError(f"ČNB feed unreachable: {exc}") from exc

    # The feed is served as ISO-8859-2/windows-1250 depending on locale; the
    # fields we parse are ASCII either way, so decode leniently.
    resp.encoding = resp.encoding or "utf-8"
    rates, rates_date = parse(resp.text)

    if not rates:
        # Non-publishing day with an explicit date: walk back up to a week.
        probe = (day or dt.date.today()) - dt.timedelta(days=1)
        for _ in range(7):
            found = _try_day(probe)
            if found is not None:
                return found
            probe -= dt.timedelta(days=1)
        raise SourceError("ČNB feed returned no rates for the last 7 days")

    return FxRates(rates, rates_date)


def _try_day(day: dt.date) -> FxRates | None:
    try:
        resp = requests.get(
            FEED_URL, params={"date": day.strftime("%d.%m.%Y")}, timeout=TIMEOUT
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None
    rates, rates_date = parse(resp.text)
    return FxRates(rates, rates_date) if rates else None


def parse(text: str) -> tuple[dict[str, Decimal], str]:
    """Parse the pipe-delimited feed into {code: CZK per 1 unit}."""
    rates: dict[str, Decimal] = {}
    rates_date = ""
    for index, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line:
            continue
        if index == 0:
            # "31 Jul 2026 #146"
            rates_date = line.split("#")[0].strip()
            continue
        parts = line.split("|")
        if len(parts) != 5:
            continue
        _country, _currency, amount, code, rate = (p.strip() for p in parts)
        if code.upper() == "CODE":  # header row
            continue
        try:
            per_unit = Decimal(rate.replace(",", ".")) / Decimal(amount)
        except (ArithmeticError, ValueError):
            continue
        rates[code.upper()] = per_unit
    return rates, rates_date
