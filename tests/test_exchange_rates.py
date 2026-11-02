import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from retailflow.exchange_rates import ExchangeRateError, fetch_eur_huf, huf_minor_to_eur_minor

FIXTURE = (Path(__file__).parent / "fixtures/frankfurter_eur_huf_2026-10-30.json").read_bytes()


@pytest.mark.parametrize("requested", [date(2026, 10, 30), date(2026, 10, 31)])
def test_rate_and_weekend_fallback(requested: date) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params == httpx.QueryParams(
            date=requested.isoformat(), base="EUR", quotes="HUF", providers="ecb"
        )
        return httpx.Response(200, content=FIXTURE)

    result = fetch_eur_huf(requested, transport=httpx.MockTransport(handler))
    assert result.requested_date == requested
    assert result.rate_date == date(2026, 10, 30)
    assert result.rate == Decimal("366.33")
    assert result.provider == "ECB"


@pytest.mark.parametrize("failure", [429, 500, 502, 503, 504, "timeout"])
def test_transient_failure_retried(failure: int | str) -> None:
    calls = 0
    waits = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("Test timeout", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, content=FIXTURE)

    fetch_eur_huf(date(2026, 10, 30), transport=httpx.MockTransport(handler), sleep=waits.append)
    assert calls == 2
    assert waits == [1]


@pytest.mark.parametrize("status,attempts", [(400, 1), (404, 1), (422, 1), (503, 3)])
def test_http_failures_are_bounded(status: int, attempts: int) -> None:
    calls = []
    waits = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_eur_huf(
            date(2026, 10, 30), transport=httpx.MockTransport(handler), sleep=waits.append
        )
    assert len(calls) == attempts
    assert waits == ([1, 2] if attempts == 3 else [])


def test_retry_after_wait_is_capped() -> None:
    waits = []
    transport = httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "999"}))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_eur_huf(date(2026, 10, 30), transport=transport, sleep=waits.append)
    assert waits == [30, 30]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [{"date": "2026-11-01", "base": "EUR", "quote": "HUF", "rate": 400}],
        [{"date": "2026-10-30", "base": "HUF", "quote": "EUR", "rate": 400}],
        [{"date": "2026-10-30", "base": "EUR", "quote": "HUF", "rate": 0}],
        [{"date": "2026-10-30", "base": "EUR", "quote": "HUF", "rate": "NaN"}],
        [{"date": "2026-10-30", "base": "EUR", "quote": "HUF", "rate": True}],
        [{}],
        "not an observation",
    ],
)
def test_bad_response_not_retried(payload: object) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=json.dumps(payload))

    with pytest.raises(ExchangeRateError):
        fetch_eur_huf(date(2026, 10, 31), transport=httpx.MockTransport(handler))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "amount,rate,expected",
    [(800000, "400", 2000), (1, "2", 1), (3, "2", 2), (0, "400", 0), (-1, "2", -1)],
)
def test_decimal_conversion_and_rounding(amount: int, rate: str, expected: int) -> None:
    assert huf_minor_to_eur_minor(amount, Decimal(rate)) == expected


@pytest.mark.parametrize("rate", ["0", "-1", "NaN", "Infinity"])
def test_invalid_conversion_rate(rate: str) -> None:
    with pytest.raises(ValueError):
        huf_minor_to_eur_minor(800000, Decimal(rate))
