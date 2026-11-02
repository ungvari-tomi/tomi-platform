"""Retry-safe, checkpointed API-to-blob extraction independent of Airflow."""

import base64
import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from filelock import FileLock

from retailflow.api import OrderPage
from retailflow.currencies import DATASET_VERSION, FX_QUOTES, SUPPORTED_DATASETS
from retailflow.exchange_rates import fetch_eur_huf_response, fetch_eur_rate_response
from retailflow.landing import Store, download_batch, land, land_orders


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def fingerprint(page: OrderPage) -> dict:
    return {"version": page.dataset_version, "seed": page.seed, "interval": page.interval_seconds}


def capture(response: httpx.Response) -> dict:
    response.raise_for_status()
    return {
        "url": str(response.request.url),
        "body_base64": base64.b64encode(response.content).decode("ascii"),
        "retrieved_at": datetime.now(UTC).isoformat(),
    }


def restore(saved: dict) -> httpx.Response:
    return httpx.Response(
        200,
        content=base64.b64decode(saved["body_base64"]),
        request=httpx.Request("GET", saved["url"]),
    )


def extract_orders(
    store: Store,
    client: httpx.Client,
    state: Path,
    run_id: str,
    *,
    initial_after_id: int | None = None,
    expected_version: str | None = None,
) -> dict:
    """Freeze each run's upper bound; commit cursor only after a verified manifest.

    Airflow retries the task on transient HTTP/storage failures. Staged responses
    survive task retries and process/container restarts on the state volume.
    """
    state.mkdir(parents=True, exist_ok=True)
    run_key = hashlib.sha256(run_id.encode()).hexdigest()[:32]
    with FileLock(str(state / "orders.lock"), timeout=5):
        checkpoint_path = state / "orders-checkpoint.json"
        run_path = state / f"orders-{run_key}.json"
        run = load(run_path)
        if run and expected_version and run["identity"]["version"] != expected_version:
            raise ValueError("Saved run differs from expected generator version")
        if run and run.get("complete"):
            for manifest in run["manifests"]:
                download_batch(store, manifest)
            return run
        checkpoint = load(checkpoint_path)
        if run is None:
            response = client.get("/v1/orders", params={"limit": 20})
            response.raise_for_status()
            latest = OrderPage.model_validate(response.json())
            if expected_version and latest.dataset_version != expected_version:
                raise ValueError("API differs from expected generator version")
            identity = fingerprint(latest)
            if checkpoint and checkpoint["identity"] != identity:
                raise ValueError("Orders dataset settings changed; explicitly reset the cursor")
            if initial_after_id is not None and checkpoint is not None:
                raise ValueError("Initial cursor applies only before a checkpoint exists")
            cursor = (
                checkpoint["cursor"]
                if checkpoint
                else (
                    initial_after_id
                    if initial_after_id is not None
                    else max(0, latest.latest_available_id - 20)
                )
            )
            if cursor < 0 or cursor > latest.latest_available_id:
                raise ValueError("Initial cursor is outside the available dataset")
            run = {
                "identity": identity,
                "cursor": cursor,
                "target": latest.latest_available_id,
                "manifests": [],
                "complete": False,
            }
            save(run_path, run)
        if checkpoint and checkpoint["identity"] != run["identity"]:
            raise ValueError("Checkpoint identity differs from saved run")
        pages = 0
        while run["cursor"] < run["target"]:
            if pages >= 2000:
                raise ValueError(
                    "Run page safety limit reached; retry continues from saved progress"
                )
            cursor = run["cursor"]
            stage = state / f"orders-{run_key}-{cursor}.response.json"
            saved = load(stage)
            if saved is None:
                saved = capture(
                    client.get(
                        "/v1/orders",
                        params={"after_id": cursor, "limit": min(100, run["target"] - cursor)},
                    )
                )
                save(stage, saved)
            response = restore(saved)
            page = OrderPage.model_validate(response.json())
            ids = [row.order_id for row in page.orders]
            if fingerprint(page) != run["identity"]:
                raise ValueError("Orders dataset changed during extraction")
            if not ids or ids != list(range(cursor + 1, ids[-1] + 1)) or ids[-1] > run["target"]:
                raise ValueError("Orders pagination has a gap, duplicate, or out-of-range record")
            if page.next_after_id != ids[-1]:
                raise ValueError("Orders cursor does not match returned records")
            batch_id = hashlib.sha256(f"orders:{run_id}:{cursor}".encode()).hexdigest()[:32]
            manifest = land_orders(
                store,
                response,
                batch_id=batch_id,
                retrieved_at=datetime.fromisoformat(saved["retrieved_at"]),
            )
            checkpoint = load(checkpoint_path)
            save(
                checkpoint_path,
                {
                    "identity": run["identity"],
                    "cursor": max(ids[-1], checkpoint["cursor"] if checkpoint else 0),
                },
            )
            run["cursor"] = ids[-1]
            run["manifests"].append(manifest)
            save(run_path, run)
            pages += 1
        run["complete"] = True
        save(run_path, run)
        return run


def extract_rates(
    store: Store, state: Path, run_id: str, requested_date: date, *, quote: str = "HUF"
) -> str:
    if quote not in FX_QUOTES:
        raise ValueError("Unsupported FX quote")
    state.mkdir(parents=True, exist_ok=True)
    identity = f"fx:{run_id}:{requested_date}" + ("" if quote == "HUF" else f":{quote}")
    key = hashlib.sha256(identity.encode()).hexdigest()[:32]
    with FileLock(str(state / "rates.lock"), timeout=5):
        stage = state / f"rates-{key}.response.json"
        saved = load(stage)
        if saved is None:
            observation, response = (
                fetch_eur_huf_response(requested_date)
                if quote == "HUF"
                else fetch_eur_rate_response(requested_date, quote=quote)
            )
            saved = {**capture(response), "observation": observation.model_dump(mode="json")}
            save(stage, saved)
        return land(
            store,
            source="exchange-rates",
            payload=base64.b64decode(saved["body_base64"]),
            extension="json",
            request={"url": saved["url"]},
            source_metadata=saved["observation"],
            retrieved_at=datetime.fromisoformat(saved["retrieved_at"]),
            batch_id=key,
        )


def scheduled_orders(run_id: str) -> dict:
    from retailflow.blob_storage import BlobStore

    url = os.environ.get("RETAILFLOW_ORDERS_URL", "http://127.0.0.1:8000")
    if url not in {"http://127.0.0.1:8000", "http://retailflow-orders-api:8000"}:
        raise ValueError("Unapproved local orders endpoint")
    store = BlobStore()
    try:
        store.ensure_container()
        with httpx.Client(base_url=url, timeout=10, trust_env=False) as client:
            version = os.environ.get("RETAILFLOW_DATASET_VERSION", DATASET_VERSION)
            if version not in SUPPORTED_DATASETS:
                raise ValueError("Unsupported scheduled generator version")
            state = Path(os.environ["RETAILFLOW_STATE_DIR"])
            if version != "orders-v2":
                state = state / version
            return extract_orders(store, client, state, run_id, expected_version=version)
    finally:
        store.close()


def scheduled_rates(run_id: str, requested_date: str) -> str:
    from retailflow.blob_storage import BlobStore

    store = BlobStore()
    try:
        store.ensure_container()
        return extract_rates(
            store,
            Path(os.environ["RETAILFLOW_STATE_DIR"]),
            run_id,
            date.fromisoformat(requested_date),
        )
    finally:
        store.close()


def scheduled_all_rates(run_id: str, requested_date: str) -> list[str]:
    from retailflow.blob_storage import BlobStore

    store = BlobStore()
    try:
        store.ensure_container()
        return [
            extract_rates(
                store,
                Path(os.environ["RETAILFLOW_STATE_DIR"]),
                run_id,
                date.fromisoformat(requested_date),
                quote=quote,
            )
            for quote in FX_QUOTES
        ]
    finally:
        store.close()
