"""Fetch ECB EUR-base European reference rates via Frankfurter; no warehouse writes."""

import argparse
import json
import logging
import time
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext

import httpx
from pydantic import BaseModel, Field

from retailflow.currencies import FX_QUOTES

URL = "https://api.frankfurter.dev/v2/rates"
LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ExchangeRateError(ValueError):
    """The provider did not return a usable EUR/HUF observation."""


class ExchangeRate(BaseModel):
    requested_date: date
    rate_date: date
    base: str = "EUR"
    quote: str = "HUF"
    rate: Decimal = Field(gt=0, allow_inf_nan=False)
    source: str = "Frankfurter"
    provider: str = "ECB"


def _parse_rate(response: httpx.Response, requested: date, quote: str = "HUF") -> ExchangeRate:
    try:
        # Preserve the provider's decimal lexeme instead of parsing through binary float.
        rows = json.loads(response.content, parse_float=Decimal)
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("Expected exactly one observation")
        row = rows[0]
        if row["base"] != "EUR" or row["quote"] != quote:
            raise ValueError("Unexpected currency pair")
        if isinstance(row["rate"], bool):
            raise ValueError("Boolean rate is invalid")
        observation = ExchangeRate(
            requested_date=requested, rate_date=row["date"], rate=row["rate"], quote=quote
        )
        if observation.rate_date > requested:
            raise ValueError("Rate date follows requested date")
        return observation
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise ExchangeRateError(f"Invalid EUR/{quote} response from Frankfurter") from exc


def fetch_eur_huf(
    requested_date: date,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> ExchangeRate:
    return fetch_eur_huf_response(requested_date, transport=transport, sleep=sleep)[0]


def fetch_eur_huf_response(
    requested_date: date,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[ExchangeRate, httpx.Response]:
    return fetch_eur_rate_response(requested_date, quote="HUF", transport=transport, sleep=sleep)


def fetch_eur_rate_response(
    requested_date: date,
    *,
    quote: str,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[ExchangeRate, httpx.Response]:
    """Return the provider's on-or-before-date rate; retry transient failures only."""
    if quote not in FX_QUOTES:
        raise ValueError("Unsupported EUR reference currency")
    with httpx.Client(timeout=10, transport=transport, trust_env=False) as client:
        for attempt in range(3):
            try:
                response = client.get(
                    URL,
                    params={
                        "date": requested_date.isoformat(),
                        "base": "EUR",
                        "quotes": quote,
                        "providers": "ecb",
                    },
                )
            except httpx.TransportError:
                if attempt == 2:
                    raise
                status = "transport_error"
                delay = float(2**attempt)
            else:
                if response.status_code not in RETRYABLE_STATUS or attempt == 2:
                    response.raise_for_status()
                    return _parse_rate(response, requested_date, quote), response
                status = str(response.status_code)
                delay = float(2**attempt)
                # Honor numeric Retry-After within a bounded wait budget.
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = min(30, int(retry_after))
            LOGGER.warning(
                json.dumps(
                    {
                        "event": "fx_request_retry",
                        "attempt": attempt + 1,
                        "status": status,
                        "delay_seconds": delay,
                    }
                )
            )
            sleep(delay)
    raise ExchangeRateError("No usable response")


def quote_minor_to_eur_minor(amount_minor: int, rate: Decimal) -> int:
    """Convert supported two-decimal currency minor units to EUR cents, HALF_UP.

    Both currencies have two accounting decimal places, so the factors of 100
    cancel. Convert the complete order total once to avoid per-unit rounding drift.
    """
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ValueError("Amount must be integer currency minor units")
    if not rate.is_finite() or rate <= 0:
        raise ValueError("Rate must be finite and positive")
    with localcontext() as context:
        context.prec = max(28, len(str(abs(amount_minor))) + len(rate.as_tuple().digits) + 10)
        return int((Decimal(amount_minor) / rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def huf_minor_to_eur_minor(amount_minor: int, rate: Decimal) -> int:
    """Compatibility helper for existing HUF callers."""
    return quote_minor_to_eur_minor(amount_minor, rate)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="YYYY-MM-DD")
    parser.add_argument("--quote", choices=FX_QUOTES, default="HUF")
    args = parser.parse_args()
    try:
        rate = fetch_eur_rate_response(args.date, quote=args.quote)[0]
    except (httpx.HTTPError, ExchangeRateError) as exc:
        parser.exit(1, f"Exchange-rate request failed: {exc}\n")
    print(rate.model_dump_json())


if __name__ == "__main__":
    main()
