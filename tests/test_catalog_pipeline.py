"""Catalog subprocess isolation and source-independent workflow controls."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from retailflow import catalog_pipeline


def test_extraction_child_has_only_fixed_paths_and_local_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("RETAILFLOW_CATALOG_ENABLED", "true")
    monkeypatch.setenv("RETAILFLOW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AIRFLOW_DB_PASSWORD", "test-only")
    monkeypatch.setenv("AZURITE_ACCOUNT_KEY", "must-not-inherit")
    monkeypatch.setenv("PYTHONPATH", "must-not-inherit")

    def child(command, **kwargs):
        assert command[:3] == [
            "/opt/catalog/venv/bin/python",
            "-I",
            "/opt/catalog/app/infra/mongodb/extract.py",
        ]
        assert command[-1] == hashlib.sha256(b"private-run-id").hexdigest()
        assert set(kwargs["env"]) == {
            "PATH",
            "HOME",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONNOUSERSITE",
            "RETAILFLOW_BLOB_ENDPOINT",
        }
        assert kwargs["capture_output"] and kwargs["timeout"] == 180
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"status": "replayed", "manifest": "product-catalog/test/metadata.json"}
            ),
        )

    monkeypatch.setattr(catalog_pipeline.subprocess, "run", child)
    assert catalog_pipeline.scheduled_extract("private-run-id") == {
        "manifest": "product-catalog/test/metadata.json"
    }


def test_failed_child_never_echoes_its_sensitive_output(monkeypatch, tmp_path):
    monkeypatch.setenv("RETAILFLOW_CATALOG_ENABLED", "true")
    monkeypatch.setenv("RETAILFLOW_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        catalog_pipeline.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="sensitive", stderr="sensitive"),
    )
    with pytest.raises(RuntimeError, match="withheld") as failure:
        catalog_pipeline.scheduled_extract("run")
    assert "sensitive" not in str(failure.value).replace("sensitive output withheld", "")


def test_disabled_workflow_does_not_open_source_or_warehouse(monkeypatch):
    monkeypatch.setenv("RETAILFLOW_CATALOG_ENABLED", "false")
    with pytest.raises(ValueError, match="not enabled"):
        catalog_pipeline.scheduled_extract("run")
    with pytest.raises(ValueError, match="not enabled"):
        catalog_pipeline.scheduled_load("not-used")
