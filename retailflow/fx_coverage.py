"""Report date-specific FX coverage and explicitly backfill missing request dates."""

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
from filelock import FileLock

from retailflow.blob_storage import BlobStore
from retailflow.currencies import FX_QUOTES
from retailflow.extraction import extract_rates
from retailflow.warehouse import load_manifest

MAX_RATE_AGE_DAYS = 7

COVERAGE_SQL = """
WITH required AS (
    SELECT DISTINCT CAST(TIMEZONE('UTC', created_at) AS DATE) AS order_date, currency
    FROM raw.orders WHERE currency<>'EUR'
), candidates AS (
    SELECT requested_date, quote, MAX(r.rate_date) AS rate_date
    FROM ops.fx_batch_records m
    JOIN raw.exchange_rates r USING(provider, base, quote, rate_date)
    WHERE r.provider='ECB' AND r.base='EUR'
      AND r.rate_date<=requested_date
    GROUP BY requested_date, quote
)
SELECT order_date, currency, rate_date,
       CASE WHEN rate_date IS NULL THEN 'missing_rate'
            WHEN order_date-rate_date>7 THEN 'stale_rate'
            ELSE 'covered' END AS status
FROM required LEFT JOIN candidates ON order_date=requested_date AND currency=quote
ORDER BY order_date, currency
"""


def coverage(database: Path) -> list[dict]:
    with FileLock(str(database) + ".lock", mode=0o660, timeout=60):
        with duckdb.connect(str(database), read_only=True) as connection:
            return [
                {
                    "order_date": row[0].isoformat(),
                    "currency": row[1],
                    "rate_date": row[2].isoformat() if row[2] else None,
                    "status": row[3],
                }
                for row in connection.execute(COVERAGE_SQL).fetchall()
            ]


def backfill(database: Path, store, state: Path, *, max_pairs: int = 31) -> dict:
    if not 1 <= max_pairs <= 366:
        raise ValueError("max_pairs must be between 1 and 366")
    before = coverage(database)
    needed = [row for row in before if row["status"] != "covered"]
    if any(row["currency"] not in FX_QUOTES for row in needed):
        raise ValueError("Unsupported backfill currency")
    if len(needed) > max_pairs:
        raise ValueError("Backfill exceeds max_pairs; review date scope before making API calls")
    today = datetime.now(UTC).date()
    if any(date.fromisoformat(row["order_date"]) > today for row in needed):
        raise ValueError("Refusing future-date backfill")
    loaded = []
    for row in needed:
        requested = date.fromisoformat(row["order_date"])
        # Existing capture-before-upload staging makes retries preserve exact response bytes.
        manifest = extract_rates(store, state, "historical-fx-v1", requested, quote=row["currency"])
        result = load_manifest(store, manifest, database)
        loaded.append(
            {
                "requested_date": requested.isoformat(),
                "currency": row["currency"],
                "manifest": manifest,
                **result,
            }
        )
    return {
        "before": before,
        "attempted_pairs": len(needed),
        "loads": loaded,
        "after": coverage(database),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument(
        "--backfill", action="store_true", help="Fetch, land and load missing dates"
    )
    parser.add_argument("--state-directory", type=Path)
    parser.add_argument("--max-pairs", type=int, default=31)
    args = parser.parse_args()
    if not args.backfill:
        print(json.dumps({"coverage": coverage(args.database)}, indent=2))
        return
    if args.state_directory is None:
        parser.error("--backfill requires a persistent --state-directory")
    store = BlobStore()
    try:
        store.ensure_container()
        print(
            json.dumps(
                backfill(args.database, store, args.state_directory, max_pairs=args.max_pairs),
                indent=2,
                default=str,
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
