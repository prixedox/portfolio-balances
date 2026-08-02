"""Shared types and helpers for the balance fetchers.

Every fetcher exposes ``fetch(fx) -> Row`` and is allowed to raise. ``snapshot.py``
turns an exception into an ``error`` row so a broken source is visible in the
chart rather than silently absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# Column order of data/balances.csv. Do not reorder: Excel Power Query and the
# dashboard both consume this by position as well as by name.
COLUMNS = [
    "date",
    "source",
    "currency",
    "cash",
    "positions_value",
    "total_native",
    "total_czk",
    "status",
    "note",
]

STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_ERROR = "error"


class SourceError(Exception):
    """Raised by a fetcher when it cannot produce a balance."""


@dataclass
class Row:
    """One source's balance on one day."""

    date: str
    source: str
    currency: str = ""
    cash: Decimal = Decimal(0)
    positions_value: Decimal = Decimal(0)
    total_czk: Decimal = Decimal(0)
    status: str = STATUS_OK
    note: str = ""
    # Set by a fetcher that wants to override the derived total (rare).
    total_native_override: Decimal | None = field(default=None, repr=False)

    @property
    def total_native(self) -> Decimal:
        if self.total_native_override is not None:
            return self.total_native_override
        return self.cash + self.positions_value

    def to_csv_dict(self) -> dict[str, str]:
        # An error row carries no amounts. Writing 0.00 would let the chart draw
        # a line down to zero, which is exactly the smoothing-over this schema
        # exists to prevent — the row is present, the values are blank.
        blank = self.status == STATUS_ERROR
        return {
            "date": self.date,
            "source": self.source,
            "currency": self.currency,
            "cash": "" if blank else money(self.cash),
            "positions_value": "" if blank else money(self.positions_value),
            "total_native": "" if blank else money(self.total_native),
            "total_czk": "" if blank else money(self.total_czk),
            "status": self.status,
            "note": self.note,
        }


def money(value: Decimal | float | int | None) -> str:
    """Plain decimal, dot separator, two places, no symbols or grouping."""
    if value is None or value == "":
        return ""
    return f"{dec(value):.2f}"


def dec(value) -> Decimal:
    """Best-effort Decimal conversion; anything unparseable becomes 0."""
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(0)


FIAT = {"CZK", "EUR", "USD", "GBP", "PLN", "CHF", "DKK", "SEK", "NOK", "HUF"}
