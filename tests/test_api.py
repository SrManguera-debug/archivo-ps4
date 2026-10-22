from __future__ import annotations

import http.client
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote, urlencode

from ps4web.config import Config, hash_password
from ps4web.server import create_server
from tests.fake_rpi import FakeRpi
from tests.pkg_fixture import basic_fields, build_pkg


class ApiTests(unittest.TestCase):
    start_workers = True

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "library"
        self.root.mkdir()
        build_pkg(self.root / "base.pkg", basic_fields())
        build_pkg(
            self.root / "patch.pkg", basic_fields(CATEGORY="gp", VERSION="01.31")
        )
        self.fake = FakeRpi().start()
        config = Config(
            library_root=self.root,
            public_base_url="http://192.168.1.20",
            state_dir=Path(self.tmp.name) / "state",
            cache_dir=Path(self.tmp.name) / "cache",
            auth_password_hash=hash_password("secret"),
            session_secret="s" * 64,
        )
        self.server = create_server(
            config,
            host="127.0.0.1",
            port=0,
            start_workers=self.start_workers,
        )
        self.server.app.jobs.poll_interval = 0.02
        self.server.app.jobs.finish_grace = 0.2
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.csrf = ""

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.app.shutdown()
        self.thread.join(timeout=3)
        self.fake.stop()
        self.tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        csrf: bool = True,
        auth: bool = True,
        cookie: str | None = None,
    ):
        if auth and not self.cookie:
            self.login()
        headers: dict[str, str] = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        use_cookie = self.cookie if cookie is None else cookie
        if use_cookie:
            headers["Cookie"] = use_cookie
        if csrf and self.csrf and method in ("POST", "PUT", "DELETE"):
            headers["X-CSRF-Token"] = self.csrf
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        result_headers = dict(response.getheaders())
        status = response.status
        conn.close()
        set_cookie = result_headers.get("Set-Cookie")
        if set_cookie:
            match = re.search(r"ps4web_session=([^;]+)", set_cookie)
            if match:
                self.cookie = f"ps4web_session={match.group(1)}"
        try:
            content_type = result_headers.get("Content-Type", "")
            if raw and "application/json" in content_type:
                data = json.loads(raw)
            elif raw:
                try:
                    data = raw.decode("utf-8")
                except UnicodeDecodeError:
                    data = raw
            else:
                data = None
        except json.JSONDecodeError:
            data = raw
        return status, result_headers, data

    def login(self, password: str = "secret") -> None:
        status, _, data = self.request(
            "POST",
            "/api/login",
            {"password": password},
            csrf=False,
            auth=False,
            cookie="",
        )
        self.assertEqual(status, 200, data)
        self.csrf = data["csrf"]

    def add_console(self) -> dict:
        status, _, data = self.request(
            "POST",
            "/api/consoles",
            {
                "name": "PS4 Test",
                "ip": "127.0.0.1",
                "rpi_port": self.fake.port,
                "firmware": "9.00",
            },
        )
        self.assertEqual(status, 201, data)
        return data["console"]

    def test_public_and_auth_flow(self) -> None:
        status, _, _ = self.request("GET", "/", auth=False, cookie="")
        self.assertEqual(status, 303)
        status, _, data = self.request("GET", "/api/me", auth=False, cookie="")
        self.assertEqual(status, 401)
        status, _, body = self.request("GET", "/login", auth=False, cookie="")
        self.assertEqual(status, 200)
        self.assertIn("Entrar", body)
        status, _, data = self.request(
            "POST", "/api/login", {"password": "wrong"}, csrf=False, auth=False, cookie=""
        )
        self.assertEqual(status, 401)
        self.login()
        status, _, data = self.request("GET", "/api/me")
        self.assertEqual(status, 200)
        self.assertEqual(data["user"], "admin")
        self.assertEqual(data["config"]["public_base_url"], "http://192.168.1.20")
        status, _, _ = self.request("POST", "/api/logout")
        self.assertEqual(status, 200)
        self.cookie = ""
        status, _, _ = self.request("GET", "/api/me", auth=False)
        self.assertEqual(status, 401)

    def test_csrf_and_console_crud(self) -> None:
        self.login()
        status, _, data = self.request("POST", "/api/consoles", {"name": "X"}, csrf=False)
        self.assertEqual(status, 403)
        console = self.add_console()
        status, _, data = self.request("GET", "/api/consoles")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["consoles"]), 1)
        self.assertEqual(data["consoles"][0]["name"], "PS4 Test")
        status, _, data = self.request(
            "PUT", f"/api/consoles/{console['id']}", {"name": "PS4 Nuevo"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["console"]["name"], "PS4 Nuevo")
        status, _, data = self.request("GET", f"/api/consoles/{console['id']}/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["status"]["rpi"])
        self.assertEqual(data["status"]["rpi_port"], self.fake.port)
        status, _, _ = self.request("DELETE", f"/api/consoles/{console['id']}")
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", f"/api/consoles/{console['id']}")
        self.assertEqual(status, 400)

    def test_files_pkg_and_icon(self) -> None:
        self.login()
        status, _, data = self.request("GET", "/api/files")
        self.assertEqual(status, 200)
        names = [entry["name"] for entry in data["entries"]]
        self.assertIn("base.pkg", names)
        status, _, data = self.request("GET", "/api/pkg?path=base.pkg")
        self.assertEqual(status, 200)
        self.assertEqual(data["pkg"]["title"], "Test Game")
        self.assertEqual(data["pkg"]["firmware"], "10.00.80")
        status, headers, body = self.request("GET", "/api/icon?path=base.pkg")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))
        status, _, data = self.request("GET", "/api/files?path=..%2Fetc")
        self.assertEqual(status, 400)
        status, _, data = self.request("GET", "/api/files?path=missing")
        self.assertEqual(status, 400)

    def test_delete_files_and_folders(self) -> None:
        self.login()
        archive = self.root / "old.rar"
        archive.write_bytes(b"old")
        folder = self.root / "Old Game"
        folder.mkdir()
        (folder / "leftover.pkg").write_bytes(b"pkg")
        status, _, data = self.request(
            "POST",
            "/api/files/delete",
            {"paths": ["old.rar", "Old Game"]},
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(set(data["deleted"]), {"old.rar", "Old Game"})
        self.assertEqual(data["errors"], [])
        self.assertFalse(archive.exists())
        self.assertFalse(folder.exists())

    def test_delete_rejects_unsafe_paths_atomically(self) -> None:
        self.login()
        archive = self.root / "old.rar"
        archive.write_bytes(b"old")
        for paths in ([""], ["."], ["../outside"], ["missing.rar"], ["old.rar", "../outside"]):
            status, _, data = self.request(
                "POST", "/api/files/delete", {"paths": paths}
            )
            self.assertEqual(status, 400, (paths, data))
            self.assertTrue(archive.exists(), paths)

    def test_install_job_end_to_end(self) -> None:
        self.login()
        console = self.add_console()
        status, _, data = self.request(
            "POST",
            "/api/jobs",
            {
                "console_id": console["id"],
                "files": ["base.pkg"],
                "include_dlc": False,
            },
        )
        self.assertEqual(status, 201, data)
        job = data["jobs"][0]
        self.assertEqual(job["state"], "queued")
        deadline = time.time() + 8
        final = None
        while time.time() < deadline:
            status, _, listing = self.request("GET", f"/api/jobs?console_id={console['id']}")
            final = listing["jobs"][0]
            if final["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(final["state"], "done", final)
        self.assertEqual(final["progress"], 100.0)
        self.assertEqual(
            self.fake.payloads_for("/api/install"),
            [{"type": "direct", "packages": ["http://192.168.1.20/base.pkg"]}],
        )
        status, _, data = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(data["consoles"], 1)
        self.assertIn("disk", data)

    def test_extract_bad_path(self) -> None:
        self.login()
        status, _, data = self.request(
            "POST", "/api/extract", {"path": "../x.rar"}
        )
        self.assertEqual(status, 400)

    def test_jobs_include_disk_stats(self) -> None:
        self.login()
        status, _, data = self.request("GET", "/api/jobs")
        self.assertEqual(status, 200)
        self.assertIn("disk", data)
        self.assertGreater(data["disk"]["total"], 0)

    def test_discover_invalid(self) -> None:
        self.login()
        status, _, data = self.request("GET", "/api/discover?subnet=10.0.0.0%2F8")
        self.assertEqual(status, 400)


class ApiWithoutWorkersTests(ApiTests):
    start_workers = False

    def test_install_job_end_to_end(self) -> None:
        self.skipTest("workers disabled")

    def test_delete_blocks_active_extract(self) -> None:
        self.login()
        folder = self.root / "Games"
        folder.mkdir()
        archive = folder / "old.rar"
        archive.write_bytes(b"old")
        job = self.server.app.jobs.create_extract("Games/old.rar")
        status, _, data = self.request(
            "POST", "/api/files/delete", {"paths": ["Games"]}
        )
        self.assertEqual(status, 409, data)
        self.assertTrue(archive.exists())
        self.server.app.jobs.cancel(job.id)
        status, _, data = self.request(
            "POST", "/api/files/delete", {"paths": ["Games"]}
        )
        self.assertEqual(status, 200, data)
        self.assertFalse(folder.exists())

    def test_delete_blocks_active_install(self) -> None:
        self.login()
        console = self.add_console()
        status, _, data = self.request(
            "POST", "/api/jobs", {"console_id": console["id"], "files": ["base.pkg"]}
        )
        self.assertEqual(status, 201, data)
        job = data["jobs"][0]
        status, _, data = self.request(
            "POST", "/api/files/delete", {"paths": ["base.pkg"]}
        )
        self.assertEqual(status, 409, data)
        self.assertTrue((self.root / "base.pkg").exists())
        self.request("POST", f"/api/jobs/{job['id']}/cancel")
        status, _, data = self.request(
            "POST", "/api/files/delete", {"paths": ["base.pkg"]}
        )
        self.assertEqual(status, 200, data)
        self.assertFalse((self.root / "base.pkg").exists())

    def test_delete_with_active_job(self) -> None:
        self.login()
        console = self.add_console()
        status, _, data = self.request(
            "POST", "/api/jobs", {"console_id": console["id"], "files": ["base.pkg"]}
        )
        self.assertEqual(status, 201, data)
        status, _, data = self.request("DELETE", f"/api/consoles/{console['id']}")
        self.assertEqual(status, 409)
        job = self.request("GET", "/api/jobs")[2]["jobs"][0]
        self.request("POST", f"/api/jobs/{job['id']}/cancel")
        status, _, _ = self.request("DELETE", f"/api/consoles/{console['id']}")
        self.assertEqual(status, 200)

    def test_clear_jobs_removes_only_terminal_jobs(self) -> None:
        self.login()
        console = self.add_console()
        status, _, data = self.request(
            "POST",
            "/api/jobs",
            {
                "console_id": console["id"],
                "files": ["base.pkg", "patch.pkg"],
            },
        )
        self.assertEqual(status, 201, data)
        finished, active = data["jobs"]
        with self.server.app.jobs._lock:
            self.server.app.jobs.get(finished["id"]).state = "done"
            self.server.app.jobs._persist_locked()

        status, _, data = self.request(
            "POST", "/api/jobs/clear", {"console_id": console["id"]}
        )

        self.assertEqual(status, 200, data)
        self.assertEqual(data["removed"], 1)
        jobs = self.request("GET", "/api/jobs")[2]["jobs"]
        self.assertEqual([job["id"] for job in jobs], [active["id"]])
        self.assertEqual(jobs[0]["state"], "queued")


if __name__ == "__main__":
    unittest.main()
