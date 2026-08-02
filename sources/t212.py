"""Trading 212 — official public API (beta).

Auth is HTTP Basic: API key as username, API secret as password. ``requests``
builds the base64 ``Authorization: Basic ...`` header from the auth tuple.

Only Invest / Stocks ISA accounts are supported by the API, and responses are in
the account's primary currency only — multi-currency accounts are not supported.
The figures are therefore treated as already single-currency and no per-position
FX is attempted.

Rate limits are enforced per account rather than per key or IP, so retrying
tightly does not help. ``x-ratelimit-remaining`` is read and the client waits
rather than hammering.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal

import requests

from .base import STATUS_OK, Row, SourceError, dec

BASE_URL = os.environ.get("T212_BASE_URL", "https://live.trading212.com/api/v0")
TIMEOUT = 30

# The public API is rate limited per endpoint; a small fixed spacing between the
# three calls keeps a single run comfortably inside the budget.
CALL_SPACING_SECONDS = 2.0
MAX_ATTEMPTS = 3


def fetch(fx, dry_run: bool = False) -> Row:
    key = os.environ.get("T212_API_KEY")
    secret = os.environ.get("T212_API_SECRET")
    if not key or not secret:
        raise SourceError("T212_API_KEY / T212_API_SECRET not set")

    session = requests.Session()
    session.auth = (key, secret)

    info = _get(session, "/equity/account/info")
    currency = (info.get("currencyCode") or "EUR").upper()

    cash_data = _get(session, "/equity/account/cash")
    cash = dec(cash_data.get("free"))

    positions = _get(session, "/equity/portfolio")
    positions_value = Decimal(0)
    for position in positions or []:
        positions_value += dec(position.get("quantity")) * dec(
            position.get("currentPrice")
        )

    row = Row(
        date="",
        source="t212",
        currency=currency,
        cash=cash,
        positions_value=positions_value,
        status=STATUS_OK,
    )
    row.total_czk = fx.to_czk(row.total_native, currency)
    if positions:
        row.note = f"{len(positions)} positions"
    return row


def _get(session: requests.Session, path: str):
    """GET with backoff driven by the rate-limit headers rather than blind retries."""
    url = f"{BASE_URL}{path}"
    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(CALL_SPACING_SECONDS * (attempt + 1))
        try:
            resp = session.get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last_error = str(exc)
            continue

        if resp.status_code == 429:
            wait = _retry_after(resp)
            last_error = f"429 rate limited on {path}"
            time.sleep(wait)
            continue
        if resp.status_code == 401:
            raise SourceError("401 unauthorised — check T212 key/secret and scopes")
        if resp.status_code == 403:
            raise SourceError(
                "403 forbidden — API is Invest / Stocks ISA only, or key lacks scope"
            )
        if resp.status_code >= 400:
            raise SourceError(f"{resp.status_code} from {path}: {resp.text[:200]}")

        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) <= 1:
            time.sleep(_retry_after(resp))
        else:
            time.sleep(CALL_SPACING_SECONDS)

        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"non-JSON response from {path}: {exc}") from exc

    raise SourceError(f"giving up on {path}: {last_error}")


def _retry_after(resp: requests.Response) -> float:
    for header in ("retry-after", "x-ratelimit-reset"):
        value = resp.headers.get(header)
        if value and value.strip().isdigit():
            return min(float(value), 60.0)
    return 5.0
