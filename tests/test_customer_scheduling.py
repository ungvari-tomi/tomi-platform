import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_customer_privacy import MemoryStore

from retailflow import customer_scheduling
from retailflow.api import Settings, create_app
from retailflow.customer_scheduling import incremental
from retailflow.privacy import PrivacyKey, prepare_key_file


def test_incremental_bounded_start_retry_noop_and_new_orders(tmp_path):
    now = datetime(2026, 11, 4, tzinfo=UTC)
    store = MemoryStore()
    key = PrivacyKey(bytes(range(32)))
    with TestClient(create_app(Settings(), clock=lambda: now)) as client:
        store.fail_manifest_once = True
        with pytest.raises(OSError):
            incremental(store, client, tmp_path, "first", key, (42, 600))
        assert not list(tmp_path.rglob("checkpoint.json"))
        landed = dict(store.files)
        first = incremental(store, client, tmp_path, "first", key, (42, 600))
        assert landed == store.files
        payload = next(value for name, value in store.files.items() if name.endswith("orders.csv"))
        assert len(payload.splitlines()) == 21  # Header and a bounded 20-order starting window.
        assert incremental(store, client, tmp_path, "same-clock", key, (42, 600))["manifests"] == []
        now += timedelta(minutes=20)
        later = incremental(store, client, tmp_path, "later", key, (42, 600))
        assert later["cursor"] == first["cursor"] + 2
        # Retrying an older successful run must never move the cursor backward.
        incremental(store, client, tmp_path, "first", key, (42, 600))
        checkpoint = json.loads(next(tmp_path.rglob("checkpoint.json")).read_text())
        assert checkpoint["cursor"] == later["cursor"]
        with pytest.raises(ValueError, match="new key ID"):
            incremental(store, client, tmp_path, "rotation", PrivacyKey(bytes(32)), (42, 600))
    persisted = b"\n".join(path.read_bytes() for path in tmp_path.rglob("*.json"))
    assert b"example.invalid" not in persisted and b"customer_id" not in persisted


def test_wrong_source_profile_does_not_land_or_advance(tmp_path):
    with TestClient(create_app(Settings(seed=9))) as client:
        store = MemoryStore()
        with pytest.raises(ValueError, match="profile"):
            incremental(store, client, tmp_path, "bad", PrivacyKey(bytes(32)), (42, 600))
        assert not store.files and not list(tmp_path.rglob("checkpoint.json"))


def test_scheduler_masks_failure_and_rejects_external_source(monkeypatch):
    monkeypatch.setenv("RETAILFLOW_ORDERS_URL", "https://example.invalid")
    with pytest.raises(RuntimeError, match="details suppressed") as failure:
        customer_scheduling.scheduled_extract("run")
    assert failure.value.__suppress_context__


def test_prepare_key_file_is_private_idempotent_and_refuses_rotation(monkeypatch, tmp_path):
    import base64

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RETAILFLOW_CUSTOMER_PRIVACY_KEY", base64.b64encode(bytes(32)).decode())
    monkeypatch.setenv("RETAILFLOW_CUSTOMER_PRIVACY_KEY_ID", "test-v1")
    prepare_key_file()
    path = tmp_path / ".local-secrets/customer.json"
    original = path.read_bytes()
    prepare_key_file()
    assert path.read_bytes() == original
    assert PrivacyKey.from_file(path).key_id == "test-v1"
    monkeypatch.setenv("RETAILFLOW_CUSTOMER_PRIVACY_KEY_ID", "test-v2")
    with pytest.raises(ValueError, match="rotation review"):
        prepare_key_file()
