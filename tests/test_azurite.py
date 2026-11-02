"""Opt-in tests against the real local emulator, never an Azure cloud account."""

import base64
import os
import secrets
from uuid import uuid4

import pytest
from azure.core.exceptions import HttpResponseError

from retailflow.blob_storage import BlobStore

pytestmark = pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_AZURITE") != "1",
    reason="Set RETAILFLOW_TEST_AZURITE=1 with local Azurite running",
)


def test_live_blob_roundtrip_and_no_overwrite() -> None:
    store = BlobStore()
    try:
        store.ensure_container()
        name = f"checks/integration/{uuid4().hex}.txt"
        store.put(name, b"first", "text/plain")
        assert store.read(name) == b"first"
        store.put(name, b"first", "text/plain")
        with pytest.raises(ValueError, match="refusing overwrite"):
            store.put(name, b"second", "text/plain")
        assert store.read(name) == b"first"
        assert (
            store.container.get_blob_client(name)
            .get_blob_properties()
            .content_settings.content_type
            == "text/plain"
        )
    finally:
        store.close()


def test_live_wrong_key_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURITE_ACCOUNT_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    store = BlobStore()
    try:
        with pytest.raises(HttpResponseError) as error:
            store.read("checks/roundtrip.txt")
        assert error.value.status_code == 403
    finally:
        store.close()
