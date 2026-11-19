"""Catalog contracts remain deterministic and compatible with deployed source IDs."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from infra.mongodb.catalog import Catalog, connect, contract, documents, init_secrets
from retailflow.api import Settings, generate_order
from retailflow.currencies import COUNTRY_PROFILES
from retailflow.event_contracts import generate


def test_catalog_covers_existing_orders_inventory_and_market_contracts():
    products = documents()
    assert products == documents() and len(products) == 20
    ids = {doc["_id"] for doc in products}
    assert ids == {f"product-{number:03d}" for number in range(1, 21)}
    assert {doc["category"] for doc in products} == {"electronics", "apparel", "homeware"}
    assert len({doc["sku"] for doc in products}) == 20
    assert len({v["sku"] for doc in products for v in doc["variants"]}) == 40
    for doc in products:
        assert set(doc["markets"]) == set(COUNTRY_PROFILES)
        assert doc["synthetic"] is True and doc["schema_version"] == doc["revision"] == 1
        assert doc["created_at"] == doc["updated_at"] < datetime(2025, 1, 1, tzinfo=UTC)
    for identifier in range(1, 101):
        assert generate_order(identifier, Settings()).product_id in ids
        assert generate("inventory-v1", identifier).product_id in ids


def test_server_contract_is_generated_from_the_same_model():
    root = Path(__file__).resolve().parents[1]
    assert json.loads((root / "infra/mongodb/contract.json").read_text()) == contract()
    validator = contract()["validator"]["$and"][0]["$jsonSchema"]
    assert validator["additionalProperties"] is False
    assert len(validator["oneOf"]) == 3
    assert contract()["validationLevel"] == "strict" and contract()["validationAction"] == "error"


def test_credentials_are_unique_not_replaced_and_endpoints_fail_closed(tmp_path, capsys):
    directory = tmp_path / "secrets"
    init_secrets(directory)
    values = [
        json.loads((directory / (role + ".json")).read_text())
        for role in ["reader", "writer", "admin"]
    ]
    unique_passwords = len({value["password"] for value in values})
    assert unique_passwords == 3
    assert all(len(value["password"]) >= 40 for value in values)
    with pytest.raises(ValueError, match="replacement"):
        init_secrets(directory)
    assert capsys.readouterr().out == ""
    with pytest.raises(ValueError, match="local MongoDB"):
        connect("reader", directory / "reader.json", host="remote.example.invalid")
    with pytest.raises(ValueError, match="identity"):
        connect("writer", directory / "reader.json")


def test_crud_updates_are_compare_and_swap_and_do_not_accept_identity_changes():
    collection = Mock()
    collection.update_one.return_value = SimpleNamespace(modified_count=1)
    catalog = Catalog(SimpleNamespace(retailflow=SimpleNamespace(products=collection)))
    now = datetime(2026, 11, 19, tzinfo=UTC)
    assert catalog.retire("product-001", 1, now=now)
    query, update = collection.update_one.call_args.args
    assert query == {"_id": "product-001", "revision": 1, "updated_at": {"$lt": now}}
    assert update == {"$set": {"status": "retired", "updated_at": now}, "$inc": {"revision": 1}}
    with pytest.raises(ValueError, match="mutable"):
        catalog.revise("product-001", 1, {"sku": "RF-0002"}, now=now)
    with pytest.raises(ValueError, match="UTC-aware"):
        catalog.revise("product-001", 1, {"status": "retired"}, now=now.replace(tzinfo=None))
    collection.update_one.return_value = SimpleNamespace(modified_count=0)
    assert not catalog.retire("product-001", 1, now=now)


def test_query_operator_objects_cannot_replace_identifiers_or_revisions():
    collection = Mock()
    catalog = Catalog(SimpleNamespace(retailflow=SimpleNamespace(products=collection)))
    with pytest.raises(ValueError, match="identifier"):
        catalog.get({"$ne": None})
    with pytest.raises(ValueError, match="integer revision"):
        catalog.retire("product-001", {"$gt": 0}, now=datetime.now(UTC))
    collection.find_one.assert_not_called()
    collection.update_one.assert_not_called()
