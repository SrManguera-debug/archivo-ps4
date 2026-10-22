"""A fake RPI HTTP server for tests and local development."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeRpi:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.requested_port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.lock = threading.RLock()
        self.requests: list[dict[str, Any]] = []
        self.packages: list[str] = []
        self.task_id = 11
        self.paused = False
        self.stopped = False
        self.polls = 0
        self.total = 1000
        self.transferred = 0
        self.local_copy = -1
        self.error_code = 0
        self.install_status = "success"
        self.progress_status = "success"
        self.install_delay = 0.0
        self.progress_overrides: list[dict[str, Any]] = []

    @property
    def port(self) -> int:
        if self.httpd is None:
            raise RuntimeError("FakeRpi is not started")
        return self.httpd.server_address[1]

    def start(self) -> "FakeRpi":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def _send(self, status: int, payload: dict[str, Any] | str) -> None:
                if isinstance(payload, dict):
                    body = json.dumps(payload).encode()
                    content_type = "application/json"
                else:
                    body = str(payload).encode()
                    content_type = "text/plain"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._send(200, "fake rpi")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    self._send(400, {"status": "fail", "message": "invalid json"})
                    return
                with fake.lock:
                    fake.requests.append(
                        {
                            "path": self.path,
                            "payload": payload,
                            "headers": {key.lower(): value for key, value in self.headers.items()},
                            "time": time.time(),
                        }
                    )
                if fake.install_delay:
                    time.sleep(fake.install_delay)
                if self.path == "/api/install":
                    fake.packages = list(payload.get("packages") or [])
                    fake.stopped = False
                    fake.paused = False
                    fake.transferred = 0
                    fake.local_copy = -1
                    if fake.install_status != "success":
                        self._send(200, {"status": "fail", "error_code": 0x1234})
                    else:
                        self._send(
                            200,
                            {
                                "status": "success",
                                "task_id": fake.task_id,
                                "title": "Fake Game",
                            },
                        )
                    return
                if self.path == "/api/get_task_progress":
                    if fake.progress_overrides:
                        with fake.lock:
                            payload = fake.progress_overrides.pop(0)
                        self._send(200, payload)
                        return
                    with fake.lock:
                        fake.polls += 1
                        if fake.stopped:
                            self._send(200, {"status": "fail", "error": 0x1})
                            return
                        if not fake.paused:
                            fake.transferred = min(fake.total, fake.transferred + 250)
                            if fake.transferred >= fake.total:
                                if fake.local_copy < 0:
                                    fake.local_copy = 50
                                elif fake.local_copy < 100:
                                    fake.local_copy = 100
                        total = fake.total
                        transferred = fake.transferred
                        local_copy = fake.local_copy
                    # Deliberately emit invalid JSON hex literals, like flatz RPI.
                    body = (
                        "{"
                        f'"status":"{fake.progress_status}",'
                        f'"bits":0x0,"error":{fake.error_code},'
                        f'"length":0x{total:x},"transferred":0x{transferred:x},'
                        f'"length_total":0x{total:x},"transferred_total":0x{transferred:x},'
                        f'"num_index":0x1,"num_total":0x1,'
                        f'"rest_sec":0x2,"rest_sec_total":0x2,'
                        f'"preparing_percent":-1,"local_copy_percent":{local_copy}'
                        "}"
                    )
                    self._send_raw(body)
                    return
                if self.path in ("/api/pause_task", "/api/resume_task", "/api/stop_task"):
                    with fake.lock:
                        if self.path == "/api/pause_task":
                            fake.paused = True
                        elif self.path == "/api/resume_task":
                            fake.paused = False
                        else:
                            fake.stopped = True
                    self._send(200, {"status": "success"})
                    return
                if self.path == "/api/is_exists":
                    self._send(200, {"status": "success", "exists": True})
                    return
                self._send(404, {"status": "fail", "message": "not found"})

            def _send_raw(self, body: str) -> None:
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer((self.host, self.requested_port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.httpd = None
        self.thread = None

    def request_paths(self) -> list[str]:
        with self.lock:
            return [item["path"] for item in self.requests]

    def payloads_for(self, path: str) -> list[dict[str, Any]]:
        with self.lock:
            return [item["payload"] for item in self.requests if item["path"] == path]

    def wait_for_path(self, path: str, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path in self.request_paths():
                return True
            time.sleep(0.01)
        return False

    def __enter__(self) -> "FakeRpi":
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.stop()
