import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from retailflow import extraction
from retailflow.api import EPOCH, Settings, generate_order
from retailflow.blob_storage import local_endpoint


class Store:
    def __init__(self) -> None:
        self.blobs = {}
        self.fail = False

    def put(self, name: str, payload: bytes, content_type: str) -> None:
        if name in self.blobs and self.blobs[name] != payload:
            raise ValueError("Overwrite")
        self.blobs[name] = payload
        if self.fail and name.endswith("metadata.json"):
            self.fail = False
            raise OSError("Simulated acknowledgement loss after metadata upload")

    def read(self, name: str) -> bytes:
        return self.blobs[name]


class Source:
    def __init__(self, latest: int = 250) -> None:
        self.latest = latest
        self.seed = 42
        self.calls = 0
        self.bad = False

    def request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        limit = int(request.url.params.get("limit", "20"))
        after = int(request.url.params.get("after_id", max(0, self.latest - limit)))
        ids = list(range(after + 1, min(self.latest + 1, after + limit + 1)))
        if self.bad and request.url.params.get("after_id") is not None:
            ids = ids[1:]
        records = [generate_order(index, Settings(seed=self.seed)) for index in ids]
        return httpx.Response(
            200,
            json={
                "dataset_version": "orders-v2",
                "seed": self.seed,
                "interval_seconds": 600,
                "observed_at": datetime.now(UTC).isoformat(),
                "latest_available_id": self.latest,
                "latest_available_at": (
                    EPOCH + timedelta(seconds=(self.latest - 1) * 600)
                ).isoformat(),
                "data_age_seconds": 0,
                "orders": [row.model_dump(mode="json") for row in records],
                "next_after_id": ids[-1] if ids else after,
                "has_more": bool(ids and ids[-1] < self.latest),
            },
        )


def test_incremental_pagination_and_completed_run_replay(tmp_path: Path) -> None:
    source, store = Source(), Store()
    with httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(source.request)
    ) as client:
        first = extraction.extract_orders(store, client, tmp_path, "first", initial_after_id=0)
        assert first["cursor"] == 250
        assert len(first["manifests"]) == 3
        calls, blobs = source.calls, dict(store.blobs)
        assert extraction.extract_orders(store, client, tmp_path, "first") == first
        assert source.calls == calls
        assert store.blobs == blobs
        source.latest = 255
        second = extraction.extract_orders(store, client, tmp_path, "second")
        assert second["cursor"] == 255
        assert len(second["manifests"]) == 1
        assert json.loads((tmp_path / "orders-checkpoint.json").read_text())["cursor"] == 255


def test_retry_after_upload_acknowledgement_loss_is_idempotent(tmp_path: Path) -> None:
    source, store = Source(), Store()
    store.fail = True
    with httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(source.request)
    ) as client:
        with pytest.raises(OSError):
            extraction.extract_orders(store, client, tmp_path, "retry")
        assert not (tmp_path / "orders-checkpoint.json").exists()
        original = dict(store.blobs)
        calls = source.calls
        source.latest = 300
        result = extraction.extract_orders(store, client, tmp_path, "retry")
        assert result["cursor"] == 250
        assert source.calls == calls
        assert store.blobs == original


def test_default_bootstrap_and_dataset_change_rejection(tmp_path: Path) -> None:
    source, store = Source(), Store()
    with httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(source.request)
    ) as client:
        result = extraction.extract_orders(store, client, tmp_path, "first")
        assert len(result["manifests"]) == 1
        manifest = json.loads(store.blobs[result["manifests"][0]])
        assert manifest["source_metadata"]["row_count"] == 20
        source.seed = 7
        with pytest.raises(ValueError, match="settings changed"):
            extraction.extract_orders(store, client, tmp_path, "next")


def test_pagination_gap_never_advances_checkpoint(tmp_path: Path) -> None:
    source, store = Source(), Store()
    source.bad = True
    with httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(source.request)
    ) as client:
        with pytest.raises(ValueError, match="pagination"):
            extraction.extract_orders(store, client, tmp_path, "bad")
    assert not (tmp_path / "orders-checkpoint.json").exists()
    assert not store.blobs


def test_rates_retry_reuses_original_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    body = b'[{"date":"2026-10-30","base":"EUR","quote":"HUF","rate":366.33}]'
    from retailflow.exchange_rates import fetch_eur_huf_response

    def fetch(requested: date):
        calls.append(requested)
        return fetch_eur_huf_response(
            requested, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
        )

    monkeypatch.setattr(extraction, "fetch_eur_huf_response", fetch)
    store = Store()
    store.fail = True
    with pytest.raises(OSError):
        extraction.extract_rates(store, tmp_path, "rates", date(2026, 10, 31))
    original = dict(store.blobs)
    extraction.extract_rates(store, tmp_path, "rates", date(2026, 10, 31))
    assert len(calls) == 1
    assert original == store.blobs
    assert any(payload == body for payload in store.blobs.values())


@pytest.mark.parametrize(
    "endpoint", ["https://example.blob.core.windows.net", "http://evil:10000/retailflow"]
)
def test_cloud_or_unapproved_blob_endpoint_is_rejected(endpoint: str) -> None:
    with pytest.raises(ValueError, match="approved local"):
        local_endpoint(endpoint)
