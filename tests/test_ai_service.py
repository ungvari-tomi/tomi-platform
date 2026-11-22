"""Real HTTP boundary with verified disposable snapshots and no model network."""

import json
import threading
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest
from test_ai_tools import tools as tools
from test_catalog_reporting import warehouse as warehouse
from test_reporting import sales_warehouse as sales_warehouse

from retailflow_ai.agent import Agent
from retailflow_ai.providers import MockProvider
from retailflow_ai.server import AnalystServer


@contextmanager
def running(tools):
    server = AnalystServer(("127.0.0.1", 0), Agent(MockProvider()), tools.reports.root)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def call(server, path, value=None):
    data = json.dumps(value).encode() if value is not None else None
    request = Request(
        f"http://127.0.0.1:{server.server_port}" + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
        return json.load(response)


def test_private_http_conversation_and_evidence(tools):
    with running(tools) as server:
        assert call(server, "/health")["provider"] == "mock"
        assert (
            call(server, "/metadata")["catalog"]["sales_profiles"][0]["dataset_id"] == "profile-a"
        )
        request = {
            "question": "Which countries generated the most sales?",
            "context": {
                "source": "catalog",
                "dataset_id": "profile-a",
                "start_date": "2026-11-19",
                "end_date": "2026-11-19",
            },
        }
        answer = call(server, "/chat", request)
        assert answer["status"] == "answered" and "[e1]" in answer["text"]
        assert answer["evidence"]["e1"]["facts"]["totals"]["orders"] == 3
        assert "private_sentinel" not in json.dumps(answer)
        request["history"] = [{"role": "user", "content": request["question"]}]
        request["question"] = "What about Hungary?"
        answer = call(server, "/chat", request)
        assert answer["trace"][0]["arguments"]["countries"] == ["HU"]
        assert answer["evidence"]["e1"]["facts"]["totals"]["covered_revenue_eur_minor"] is None


def test_http_rejects_paths_oversize_unknown_fields_and_busy(tools):
    with running(tools) as server:
        for path, value, expected in [
            ("/.env", None, 404),
            ("/chat", {"question": "x", "root": "/private"}, 400),
            ("/chat", {"question": "x" * 40_000}, 400),
        ]:
            with pytest.raises(HTTPError) as error:
                call(server, path, value)
            assert error.value.code == expected
            assert b"private" not in error.value.read()
        with server.busy:
            with pytest.raises(HTTPError) as error:
                call(server, "/metadata")
            assert error.value.code == 429
