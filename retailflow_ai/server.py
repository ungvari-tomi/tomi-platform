"""Private, single-analysis HTTP boundary; no file paths or provider options in requests."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from retailflow_ai.agent import Agent, Question
from retailflow_ai.providers import JsonProvider, MockProvider
from retailflow_ai.snapshots import Reports


class AnalystServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 4

    def __init__(self, address, agent: Agent, root: Path):
        self.agent, self.root = agent, root
        self.busy = threading.Lock()
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server_version = "RetailFlowAnalyst"

    def log_message(self, *args):
        # Never log questions, model output, headers or filesystem exceptions.
        pass

    def reply(self, status: int, value: dict):
        payload = json.dumps(value).encode()
        if len(payload) > 256_000:
            status, payload = 503, b'{"error":"response_limit"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok", "provider": self.server.agent.provider.name})
        elif self.path == "/metadata":
            self.guarded(metadata=True)
        else:
            self.reply(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/chat":
            self.reply(404, {"error": "not_found"})
            return
        self.guarded(metadata=False)

    def guarded(self, *, metadata: bool):
        server = self.server
        if not server.busy.acquire(blocking=False):
            self.reply(429, {"error": "analysis_busy"})
            return
        try:
            # Timed-out read-only workers cannot be force-killed. Admit no more work
            # until the previous worker exits; do not accumulate abandoned threads.
            if server.agent.pending is not None and not server.agent.pending.done():
                self.reply(429, {"error": "analysis_busy"})
                return
            reports = Reports(server.root)
            if metadata:
                self.reply(200, reports.metadata())
                return
            try:
                if self.headers.get("Content-Type") != "application/json":
                    raise ValueError()
                if self.headers.get("Transfer-Encoding") is not None:
                    raise ValueError()
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32_000:
                    raise ValueError()
                request = Question.model_validate_json(self.rfile.read(length))
            except Exception:
                self.reply(400, {"error": "invalid_request"})
                return
            result = server.agent.run(request, reports)
            self.reply(200, result.model_dump(mode="json"))
        except Exception:
            self.reply(503, {"error": "analysis_unavailable"})
        finally:
            server.busy.release()


def main():
    kind = os.environ.get("RETAILFLOW_AI_PROVIDER", "mock")
    if kind == "mock":
        provider = MockProvider()
    else:
        provider = JsonProvider(
            kind,
            os.environ.get("RETAILFLOW_AI_MODEL", ""),
            endpoint=os.environ.get("RETAILFLOW_AI_OLLAMA_ENDPOINT"),
            key_file=Path("/run/secrets/model-key") if kind == "openai" else None,
            allow_hosted=os.environ.get("RETAILFLOW_AI_ALLOW_HOSTED") == "true",
        )
    bind = os.environ.get("RETAILFLOW_AI_BIND", "127.0.0.1")
    if bind not in {"127.0.0.1", "0.0.0.0"}:  # nosec B104
        raise ValueError("Invalid analyst bind address")
    server = AnalystServer((bind, 8090), Agent(provider), Path("/reports"))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
