"""Coinmate — official private REST API.

    POST https://coinmate.io/api/balances
    form: clientId, publicKey, nonce, signature

    signature = HMAC-SHA256(key=privateKey, msg=nonce + clientId + publicKey)
                .hexdigest().upper()

The concatenation order and the uppercase hex are both load-bearing; getting
either wrong returns a generic "Invalid request". Verified against the official
coinmate-io/coinmate-api-examples Python client.

``nonce`` must strictly increase across calls — epoch milliseconds.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import Decimal

import requests

from .base import FIAT, STATUS_OK, Row, SourceError, dec

API_BASE = "https://coinmate.io/api"
TIMEOUT = 30
NATIVE_CURRENCY = "CZK"


def fetch(fx, dry_run: bool = False) -> Row:
    client_id = os.environ.get("COINMATE_CLIENT_ID")
    public_key = os.environ.get("COINMATE_PUBLIC_KEY")
    private_key = os.environ.get("COINMATE_PRIVATE_KEY")
    if not (client_id and public_key and private_key):
        raise SourceError(
            "COINMATE_CLIENT_ID / COINMATE_PUBLIC_KEY / COINMATE_PRIVATE_KEY not set"
        )

    balances = _post_private(
        "balances", client_id=client_id, public_key=public_key, private_key=private_key
    )

    cash = Decimal(0)
    positions_value = Decimal(0)
    held = []

    for code, entry in (balances or {}).items():
        code = code.upper()
        amount = dec(entry.get("balance") if isinstance(entry, dict) else entry)
        if amount == 0:
            continue
        if code in FIAT:
            cash += fx.to_czk(amount, code)
        else:
            price = _ticker_price(f"{code}_{NATIVE_CURRENCY}")
            positions_value += amount * price
            held.append(code)

    row = Row(
        date="",
        source="coinmate",
        currency=NATIVE_CURRENCY,
        cash=cash,
        positions_value=positions_value,
        status=STATUS_OK,
        note=("holds " + "/".join(sorted(held))) if held else "",
    )
    # Native currency is CZK, so the total is already in CZK.
    row.total_czk = row.total_native
    return row


def _post_private(endpoint: str, *, client_id: str, public_key: str, private_key: str):
    nonce = str(int(time.time() * 1000))
    message = f"{nonce}{client_id}{public_key}"
    signature = (
        hmac.new(
            private_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        )
        .hexdigest()
        .upper()
    )

    payload = {
        "clientId": client_id,
        "publicKey": public_key,
        "nonce": nonce,
        "signature": signature,
    }

    try:
        resp = requests.post(f"{API_BASE}/{endpoint}", data=payload, timeout=TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        raise SourceError(f"coinmate {endpoint} request failed: {exc}") from exc
    except ValueError as exc:
        raise SourceError(f"coinmate {endpoint} returned non-JSON: {exc}") from exc

    if body.get("error"):
        raise SourceError(f"coinmate {endpoint}: {body.get('errorMessage')}")
    return body.get("data")


def _ticker_price(pair: str) -> Decimal:
    """Public ticker — no auth needed."""
    try:
        resp = requests.get(
            f"{API_BASE}/ticker", params={"currencyPair": pair}, timeout=TIMEOUT
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise SourceError(f"coinmate ticker {pair} failed: {exc}") from exc

    if body.get("error"):
        raise SourceError(f"coinmate ticker {pair}: {body.get('errorMessage')}")
    data = body.get("data") or {}
    price = dec(data.get("last"))
    if price <= 0:
        raise SourceError(f"coinmate ticker {pair} returned no price")
    return price
