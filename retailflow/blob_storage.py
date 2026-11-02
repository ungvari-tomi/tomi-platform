"""Local-only Azurite blob access and safe emulator configuration."""

import argparse
import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import dotenv_values

ACCOUNT = "retailflow"
ENDPOINT = f"http://127.0.0.1:10000/{ACCOUNT}"
CONTAINER = "landing"


def local_endpoint(value: str) -> str:
    allowed = {ENDPOINT, f"http://tomi-platform-azurite-1:10000/{ACCOUNT}"}
    if value not in allowed:
        raise ValueError("Blob endpoint must be the approved local Azurite endpoint")
    return value


def initialize() -> None:
    """Create an emulator-only key without replacing an existing local key."""
    path = Path(".env")
    if os.environ.get("AZURITE_ACCOUNT_KEY") or dotenv_values(path).get("AZURITE_ACCOUNT_KEY"):
        return
    key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n# Local Azurite emulator only; never use in Azure.\nAZURITE_ACCOUNT_KEY={key}\n"
        )


class BlobStore:
    """Block blobs with application-level no-overwrite checks, not Azure WORM."""

    def __init__(self) -> None:
        local = dotenv_values(".env", encoding="utf-8", interpolate=False)
        key = os.environ.get("AZURITE_ACCOUNT_KEY", local.get("AZURITE_ACCOUNT_KEY"))
        if not key:
            raise ValueError("Run python -m retailflow.blob_storage init first")
        self.service = BlobServiceClient(
            account_url=local_endpoint(os.environ.get("RETAILFLOW_BLOB_ENDPOINT", ENDPOINT)),
            credential={"account_name": ACCOUNT, "account_key": key},
            api_version="2023-11-03",
            retry_total=2,
            connection_timeout=5,
            read_timeout=10,
        )
        self.container = self.service.get_container_client(CONTAINER)

    def ensure_container(self) -> None:
        try:
            self.container.create_container()
        except ResourceExistsError:
            pass

    def put(self, name: str, payload: bytes, content_type: str) -> None:
        blob = self.container.get_blob_client(name)
        try:
            blob.upload_blob(
                payload,
                overwrite=False,
                content_settings=ContentSettings(content_type=content_type),
            )
        except ResourceExistsError:
            if self.read(name) != payload:
                raise ValueError(
                    "Existing blob has different content; refusing overwrite"
                ) from None

    def read(self, name: str) -> bytes:
        return self.container.get_blob_client(name).download_blob().readall()

    def close(self) -> None:
        self.service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "check", "list"])
    args = parser.parse_args()
    if args.action == "init":
        initialize()
        print("Local Azurite key configured in .env or process environment; no key printed.")
        return
    store = BlobStore()
    try:
        store.ensure_container()
        if args.action == "list":
            for blob in store.container.list_blobs():
                print(json.dumps({"name": blob.name, "bytes": blob.size}))
        else:
            payload = b"RetailFlow Azurite upload/download check\n"
            name = "checks/roundtrip.txt"
            store.put(name, payload, "text/plain; charset=utf-8")
            if store.read(name) != payload:
                raise ValueError("Blob round-trip mismatch")
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "blob": name,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            )
    finally:
        store.close()


if __name__ == "__main__":
    main()
