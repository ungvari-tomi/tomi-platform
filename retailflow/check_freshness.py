"""Check a running service through two real HTTP requests."""

import argparse
import json
import time
from datetime import UTC, datetime

import httpx

from retailflow.api import OrderPage


def validate_freshness(page: OrderPage, now: datetime) -> None:
    age = (now - page.latest_available_at).total_seconds()
    if not -1 <= age <= page.interval_seconds * 2:
        raise ValueError(f"Source data is stale or future-dated: age={age:.2f}s")
    if not page.orders:
        raise ValueError("Expected new orders, received an empty page")
    if page.orders[-1].created_at != page.latest_available_at:
        raise ValueError("Returned orders do not reach the latest advertised timestamp")
    for order in page.orders:
        if order.total_minor != order.quantity * order.unit_price_minor:
            raise ValueError("Order total violates business rule")


def check(base_url: str, max_wait_seconds: float = 30) -> dict[str, int | str]:
    with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
        first_response = client.get("/v1/orders")
        first_response.raise_for_status()
        first = OrderPage.model_validate(first_response.json())
        validate_freshness(first, datetime.now(UTC))
        wait_seconds = first.interval_seconds + 0.25
        if wait_seconds > max_wait_seconds:
            raise ValueError(
                f"Check needs {wait_seconds}s, exceeding the {max_wait_seconds}s wait limit. "
                "Restart the API with RETAILFLOW_INTERVAL_SECONDS=5 for a quick test, "
                "or increase --max-wait-seconds explicitly."
            )
        time.sleep(wait_seconds)
        second_response = client.get("/v1/orders", params={"after_id": first.next_after_id})
        second_response.raise_for_status()
        second = OrderPage.model_validate(second_response.json())
        validate_freshness(second, datetime.now(UTC))
        if (first.seed, first.interval_seconds, first.dataset_version) != (
            second.seed,
            second.interval_seconds,
            second.dataset_version,
        ):
            raise ValueError("Source configuration changed between calls")
        if second.orders[0].order_id != first.next_after_id + 1:
            raise ValueError("New orders are not contiguous with the previous cursor")
        if second.latest_available_at <= first.latest_available_at:
            raise ValueError("Source timestamps did not advance")
        return {
            "status": "passed",
            "first_latest_id": first.latest_available_id,
            "second_latest_id": second.latest_available_id,
            "new_orders": len(second.orders),
            "latest_created_at": second.latest_available_at.isoformat(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--max-wait-seconds", type=float, default=30)
    args = parser.parse_args()
    print(json.dumps(check(args.url, args.max_wait_seconds)))


if __name__ == "__main__":
    main()
