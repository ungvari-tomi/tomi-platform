"""Bounded incremental customer extraction; only masked stages survive retries."""

import hashlib
import os
from pathlib import Path

import httpx
from filelock import FileLock

from retailflow.api import CustomerOrderPage
from retailflow.blob_storage import BlobStore
from retailflow.customer_ingestion import extract_private, load_private
from retailflow.extraction import load, save
from retailflow.landing import Store
from retailflow.privacy import PrivacyKey


def incremental(
    store: Store,
    client: httpx.Client,
    state: Path,
    run_id: str,
    key: PrivacyKey,
    profile: tuple[int, int],
) -> dict:
    # Bind checkpoints to the configured source and key ID; reject silent key replacement.
    namespace = hashlib.sha256(repr((profile, key.key_id)).encode()).hexdigest()[:32]
    directory = state / "customer-orders-v4" / namespace
    directory.mkdir(parents=True, exist_ok=True, mode=0o770)
    identity = hashlib.sha256(run_id.encode()).hexdigest()[:32]
    with FileLock(str(directory / "extraction.lock"), mode=0o660, timeout=10):
        checkpoint_path = directory / "checkpoint.json"
        checkpoint = load(checkpoint_path)
        commitment = key.token("checkpoint-key-verification")
        if checkpoint and checkpoint["key_commitment"] != commitment:
            raise ValueError("Customer key changed without a new key ID")
        record_path = directory / (identity + ".run.json")
        record = load(record_path)
        if record is None:
            # Freeze a decision for this run, including a no-op; no contact fields here.
            response = client.get("/v2/orders", params={"limit": 1})
            response.raise_for_status()
            try:
                page = CustomerOrderPage.model_validate(response.json())
            except ValueError:
                raise ValueError("Customer source contract changed") from None
            if (page.seed, page.interval_seconds) != profile:
                raise ValueError("Customer source profile differs from configuration")
            cursor = checkpoint["cursor"] if checkpoint else None
            record = {
                "after_id": cursor,
                "noop": cursor is not None and cursor >= page.latest_available_id,
                "key_commitment": commitment,
            }
            save(record_path, record)
        if record["key_commitment"] != commitment:
            raise ValueError("Customer retry key differs")
        if record["noop"]:
            return {"manifests": [], "cursor": record["after_id"]}
        manifest = extract_private(
            store,
            client,
            directory / "batches",
            run_id,
            key,
            after_id=record["after_id"],
            limit=20 if record["after_id"] is None else 100,
            expected_profile=profile,
        )
        # extract_private has downloaded and verified all files plus the last manifest.
        stage = load(directory / "batches" / (identity + ".json"))
        cursor = max(row["order_id"] for row in stage["orders"])
        save(
            checkpoint_path,
            {
                "cursor": max(cursor, checkpoint["cursor"] if checkpoint else 0),
                "key_commitment": commitment,
            },
        )
        return {"manifests": [manifest], "cursor": cursor}


def scheduled_extract(run_id: str) -> dict:
    try:
        url = os.environ.get("RETAILFLOW_ORDERS_URL", "http://127.0.0.1:18000")
        if url not in {"http://127.0.0.1:18000", "http://retailflow-orders-api:8000"}:
            raise ValueError("Unapproved customer endpoint")
        key = PrivacyKey.from_file(Path(os.environ["RETAILFLOW_CUSTOMER_PRIVACY_KEY_FILE"]))
        if key.key_id != os.environ.get("RETAILFLOW_CUSTOMER_PRIVACY_KEY_ID", "local-v1"):
            raise ValueError("Scheduler customer key ID differs from configuration")
        profile = (
            int(os.environ["RETAILFLOW_SEED"]),
            int(os.environ["RETAILFLOW_INTERVAL_SECONDS"]),
        )
        store = BlobStore()
        try:
            store.ensure_container()
            with httpx.Client(
                base_url=url, timeout=15, trust_env=False, follow_redirects=False
            ) as client:
                result = incremental(
                    store, client, Path(os.environ["RETAILFLOW_STATE_DIR"]), run_id, key, profile
                )
        finally:
            store.close()
        return result
    except Exception:
        # Airflow captures exception chains; suppress responses and rejected input values.
        raise RuntimeError("Customer extraction failed; source details suppressed") from None


def scheduled_load() -> dict:
    try:
        store = BlobStore()
        try:
            manifests = sorted(
                blob.name
                for blob in store.container.list_blobs(name_starts_with="customer-orders/")
                if blob.name.endswith("/metadata.json")
            )
            loaded = 0
            for manifest in manifests:
                result = load_private(store, manifest, Path(os.environ["RETAILFLOW_DUCKDB_PATH"]))
                loaded += int(not result["already_loaded"])
            return {"batches_loaded": loaded, "batches_skipped": len(manifests) - loaded}
        finally:
            store.close()
    except Exception:
        raise RuntimeError("Customer loading failed; source details suppressed") from None
