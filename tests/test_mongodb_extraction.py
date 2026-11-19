"""Frozen retries, BSON fidelity and revision/checkpoint enforcement."""

import copy
import json
from unittest.mock import Mock

import pytest
from bson.json_util import JSONOptions, loads

from infra.mongodb.catalog import documents
from infra.mongodb.extract import encode, extract
from retailflow.batch_landing import download_batch


class MemoryStore:
    def __init__(self):
        self.blobs = {}
        self.fail_manifest = False

    def put(self, name, payload, content_type):
        if name.endswith("metadata.json") and self.fail_manifest:
            raise OSError("Injected metadata failure")
        if name in self.blobs and self.blobs[name] != payload:
            raise ValueError("Immutable conflict")
        self.blobs[name] = payload

    def read(self, name):
        return self.blobs[name]


def source(rows=None):
    collection = Mock()
    collection.find.return_value.sort.return_value.limit.return_value = (
        documents() if rows is None else rows
    )
    return collection


def test_snapshot_preserves_bson_and_verifies_replay(tmp_path):
    store, collection = MemoryStore(), source()
    result = extract(store, collection, tmp_path, "one")
    metadata, body = download_batch(store, result["manifest"])
    assert loads(body, json_options=JSONOptions(tz_aware=True)) == documents()
    assert b'"$date"' in body and b'"$numberInt"' in body
    assert metadata["source_metadata"]["row_count"] == 20
    assert metadata["source"].startswith("product-catalog/")
    collection.find.reset_mock()
    assert extract(store, collection, tmp_path, "one")["status"] == "replayed"
    collection.find.assert_not_called()
    store.blobs[metadata["payload_blob"]] += b"corrupt"
    with pytest.raises(ValueError, match="checksum"):
        extract(store, collection, tmp_path, "one")


def test_manifest_failure_retries_frozen_bytes_before_reading_source(tmp_path):
    store, collection = MemoryStore(), source()
    store.fail_manifest = True
    with pytest.raises(OSError):
        extract(store, collection, tmp_path, "first")
    assert not (tmp_path / "checkpoint.json").exists()
    frozen = copy.deepcopy(store.blobs)
    collection.find.side_effect = AssertionError("Retry must not query source")
    store.fail_manifest = False
    recovered = extract(store, collection, tmp_path, "second")
    assert recovered["status"] == "landed"
    assert all(store.blobs[name] == payload for name, payload in frozen.items())
    assert not (tmp_path / "pending.json").exists()
    assert json.loads((tmp_path / "checkpoint.json").read_text())["sequence"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "extra",
        "classification",
        "version",
        "same_revision",
        "regression",
        "unknown_field",
    ],
)
def test_contract_and_revision_conflicts_do_not_advance_checkpoint(tmp_path, mutation):
    store = MemoryStore()
    extract(store, source(), tmp_path, "initial")
    before = (tmp_path / "checkpoint.json").read_bytes()
    rows = documents()
    if mutation == "missing":
        rows.pop()
    elif mutation == "extra":
        rows.append(copy.deepcopy(rows[0]))
    elif mutation == "classification":
        rows[0]["synthetic"] = False
    elif mutation == "version":
        rows[0]["schema_version"] = 2
    elif mutation == "same_revision":
        rows[0]["name"] = "Changed without revision"
    elif mutation == "unknown_field":
        rows[0]["customer_email"] = "synthetic@example.test"
    else:
        rows[0]["revision"] = 0
    with pytest.raises(ValueError):
        extract(store, source(rows), tmp_path, "changed")
    assert (tmp_path / "checkpoint.json").read_bytes() == before
    assert not (tmp_path / "pending.json").exists()


def test_revised_and_retired_products_remain_in_snapshot(tmp_path):
    store = MemoryStore()
    extract(store, source(), tmp_path, "initial")
    rows = documents()
    rows[0].update(revision=2, status="retired")
    result = extract(store, source(rows), tmp_path, "revision")
    assert loads(download_batch(store, result["manifest"])[1])[0]["status"] == "retired"
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    assert checkpoint["sequence"] == 2 and checkpoint["products"]["product-001"]["revision"] == 2
    old_source = source()
    assert extract(store, old_source, tmp_path, "initial")["status"] == "replayed"
    old_source.find.assert_not_called()
    assert json.loads((tmp_path / "checkpoint.json").read_text())["sequence"] == 2


def test_crash_after_checkpoint_publish_recovers_without_source_read(tmp_path):
    store = MemoryStore()
    result = extract(store, source(), tmp_path, "initial")
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text())
    (tmp_path / "pending.json").write_text(json.dumps(checkpoint["committed_pending"]))
    collection = source()
    assert extract(store, collection, tmp_path, "next")["manifest"] == result["manifest"]
    collection.find.assert_not_called()
    assert not (tmp_path / "pending.json").exists()


def test_canonical_serialization_is_stable():
    assert encode(documents()) == encode(documents())


def test_manifest_acknowledgement_loss_reuses_committed_blob_bytes(tmp_path):
    store = MemoryStore()
    put = store.put

    def lost_ack(name, payload, content_type):
        put(name, payload, content_type)
        if name.endswith("metadata.json"):
            raise OSError("Acknowledgement lost after upload")

    store.put = lost_ack
    with pytest.raises(OSError):
        extract(store, source(), tmp_path, "retry")
    frozen = copy.deepcopy(store.blobs)
    assert len(frozen) == 2 and not (tmp_path / "checkpoint.json").exists()
    store.put = put
    assert extract(store, source(), tmp_path, "retry")["status"] == "landed"
    assert store.blobs == frozen


def test_corrupt_upload_readback_never_publishes_metadata_or_checkpoint(tmp_path):
    store = MemoryStore()
    store.read = lambda name: b"corrupt"
    with pytest.raises(ValueError, match="checksum"):
        extract(store, source(), tmp_path, "corrupt")
    assert not (tmp_path / "checkpoint.json").exists()
    assert (tmp_path / "pending.json").exists()
    assert not any(name.endswith("metadata.json") for name in store.blobs)
