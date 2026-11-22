"""Launch isolated reader extraction and load only its verified snapshot."""

import hashlib
import json
import os
import subprocess  # nosec B404
from pathlib import Path

from retailflow.blob_storage import BlobStore
from retailflow.product_catalog import load_manifest


def scheduled_extract(run_id: str) -> dict:
    if os.environ.get("RETAILFLOW_CATALOG_ENABLED", "false") != "true":
        raise ValueError("Catalog workflow is not enabled")
    identity = hashlib.sha256(run_id.encode()).hexdigest()
    state = (
        Path(os.environ.get("RETAILFLOW_STATE_DIR", "/opt/airflow/state")) / "catalog-products-v1"
    )
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    result = subprocess.run(  # nosec B603
        [
            "/opt/catalog/venv/bin/python",
            "-I",
            "/opt/catalog/app/infra/mongodb/extract.py",
            "--credential-file",
            "/run/retailflow-secrets/catalog-reader.json",
            "--blob-key-file",
            "/run/retailflow-secrets/catalog-azurite-key",
            "--state",
            str(state),
            "--run-id",
            identity,
        ],
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(state),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "RETAILFLOW_BLOB_ENDPOINT": "http://tomi-platform-azurite-1:10000/retailflow",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError("Isolated catalog extraction failed; sensitive output withheld")
    summary = json.loads(result.stdout)
    if summary.get("status") not in {"landed", "replayed", "recovered"}:
        raise ValueError("Unexpected catalog extraction result")
    return {"manifest": summary["manifest"]}


def scheduled_load(manifest: str) -> dict:
    if os.environ.get("RETAILFLOW_CATALOG_ENABLED", "false") != "true":
        raise ValueError("Catalog workflow is not enabled")
    store = BlobStore()
    try:
        return load_manifest(store, manifest, Path(os.environ["RETAILFLOW_DUCKDB_PATH"]))
    finally:
        store.close()
