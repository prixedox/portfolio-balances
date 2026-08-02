"""Coinbase — official Advanced Trade API via the coinbase-advanced-py SDK.

The SDK handles JWT signing internally and auto-detects the key type, so an
Ed25519 CDP key (the recommended type) works without extra configuration. The
credentials are a CDP API key created in the Coinbase Developer Portal:

    COINBASE_API_KEY_NAME     organizations/{org}/apiKeys/{key}
    COINBASE_API_PRIVATE_KEY  the private key, newlines as \\n or real newlines

Balances are read with ``get_accounts()`` and each non-zero asset is valued from
Coinbase's own product prices. Reported in ``COINBASE_QUOTE_CURRENCY`` (EUR by
default), then converted to CZK once with the ČNB rate.
"""

from __future__ import annotations

import os
from decimal import Decimal

from .base import FIAT, STATUS_OK, Row, SourceError, dec

QUOTE_CURRENCY = os.environ.get("COINBASE_QUOTE_CURRENCY", "EUR").upper()
# Assets too small to be worth a product lookup (dust from staking rewards etc).
DUST_THRESHOLD = Decimal("0.00000001")


def fetch(fx, dry_run: bool = False) -> Row:
    key_name = os.environ.get("COINBASE_API_KEY_NAME")
    private_key = os.environ.get("COINBASE_API_PRIVATE_KEY")
    if not (key_name and private_key):
        raise SourceError("COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY not set")

    try:
        from coinbase.rest import RESTClient
    except ImportError as exc:
        raise SourceError(
            "coinbase-advanced-py not installed (pip install -r requirements.txt)"
        ) from exc

    # Repo secrets flatten newlines; the SDK needs them back.
    private_key = private_key.replace("\\n", "\n")

    try:
        client = RESTClient(api_key=key_name, api_secret=private_key)
    except Exception as exc:  # SDK raises assorted key-parsing errors
        raise SourceError(f"could not build Coinbase client: {exc}") from exc

    accounts = _all_accounts(client)

    cash = Decimal(0)
    positions_value = Decimal(0)
    held = []
    price_cache: dict[str, Decimal] = {}

    for account in accounts:
        code, amount = _balance(account)
        if not code or amount <= DUST_THRESHOLD:
            continue
        if code in FIAT:
            # Fiat sitting on the exchange is cash, converted into the quote
            # currency through CZK so one rate table covers every pair.
            cash += _cross(fx, amount, code, QUOTE_CURRENCY)
        else:
            price = _price_in_quote(client, code, price_cache, fx)
            if price is None:
                continue
            positions_value += amount * price
            held.append(code)

    row = Row(
        date="",
        source="coinbase",
        currency=QUOTE_CURRENCY,
        cash=cash,
        positions_value=positions_value,
        status=STATUS_OK,
        note=("holds " + "/".join(sorted(held))) if held else "",
    )
    row.total_czk = fx.to_czk(row.total_native, QUOTE_CURRENCY)
    return row


def _all_accounts(client) -> list:
    """Page through get_accounts(); a portfolio can exceed one page."""
    accounts = []
    cursor = None
    for _ in range(20):  # hard stop, 20 * 250 accounts is far beyond real use
        try:
            page = client.get_accounts(limit=250, cursor=cursor)
        except Exception as exc:
            raise SourceError(f"Coinbase get_accounts failed: {exc}") from exc
        batch = _attr(page, "accounts") or []
        accounts.extend(batch)
        cursor = _attr(page, "cursor")
        if not _attr(page, "has_next") or not cursor:
            break
    return accounts


def _balance(account) -> tuple[str, Decimal]:
    """available_balance + hold, as (currency, amount)."""
    available = _attr(account, "available_balance") or {}
    hold = _attr(account, "hold") or {}
    code = (_attr(available, "currency") or _attr(account, "currency") or "").upper()
    amount = dec(_attr(available, "value")) + dec(_attr(hold, "value"))
    return code, amount


def _price_in_quote(client, asset: str, cache: dict, fx) -> Decimal | None:
    """Price of one unit of ``asset`` in the quote currency, or None if unpriceable."""
    if asset in cache:
        return cache[asset]

    for quote in (QUOTE_CURRENCY, "USD", "EUR"):
        product_id = f"{asset}-{quote}"
        try:
            product = client.get_product(product_id)
        except Exception:
            continue
        price = dec(_attr(product, "price"))
        if price <= 0:
            continue
        if quote != QUOTE_CURRENCY:
            price = _cross(fx, price, quote, QUOTE_CURRENCY)
        cache[asset] = price
        return price

    cache[asset] = None
    return None


def _cross(fx, amount: Decimal, frm: str, to: str) -> Decimal:
    """Convert via CZK so a single ČNB table covers every currency pair."""
    if frm == to:
        return amount
    return fx.to_czk(amount, frm) / fx.rate(to)


def _attr(obj, name):
    """The SDK returns objects on some paths and plain dicts on others."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
