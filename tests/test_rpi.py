from __future__ import annotations

import json
import unittest

from ps4web.rpi import (
    RpiClient,
    RpiError,
    _replace_hex_literals,
    extract_task_ids,
    parse_lenient_json,
    progress_snapshot,
)
from tests.fake_rpi import FakeRpi


class LenientJsonTests(unittest.TestCase):
    def test_hex(self) -> None:
        raw = '{"length":0x10,"transferred":0xFF,"nested":{"local_copy_percent":-1}}'
        data = parse_lenient_json(raw)
        self.assertEqual(data["length"], 16)
        self.assertEqual(data["transferred"], 255)
        self.assertEqual(data["nested"]["local_copy_percent"], -1)

    def test_strings_are_untouched(self) -> None:
        raw = '{"url":"http://x/0x10","text":"a 0x20 b"}'
        data = parse_lenient_json(raw)
        self.assertEqual(data["url"], "http://x/0x10")
        self.assertEqual(data["text"], "a 0x20 b")

    def test_hex_tokenizer(self) -> None:
        self.assertEqual(_replace_hex_literals("0x10 0xAA"), "16 170")
        self.assertEqual(_replace_hex_literals('"0x10"'), '"0x10"')

    def test_invalid(self) -> None:
        with self.assertRaises(RpiError):
            parse_lenient_json("{invalid")
        with self.assertRaises(RpiError):
            parse_lenient_json("[]")

    def test_empty(self) -> None:
        self.assertEqual(parse_lenient_json(""), {})


class ProgressTests(unittest.TestCase):
    def test_download(self) -> None:
        snap = progress_snapshot(
            {
                "status": "success",
                "error": 0,
                "length_total": 1000,
                "transferred_total": 250,
                "local_copy_percent": -1,
                "preparing_percent": -1,
            }
        )
        self.assertEqual(snap["percent"], 25.0)
        self.assertEqual(snap["phase"], "downloading")
        self.assertFalse(snap["done"])
        self.assertFalse(snap["transfer_done"])

    def test_install(self) -> None:
        snap = progress_snapshot(
            {
                "status": "success",
                "length_total": 1000,
                "transferred_total": 1000,
                "local_copy_percent": 50,
            }
        )
        self.assertEqual(snap["phase"], "installing")
        self.assertEqual(snap["percent"], 50.0)
        self.assertTrue(snap["transfer_done"])

    def test_done(self) -> None:
        snap = progress_snapshot(
            {
                "status": "success",
                "length_total": 1000,
                "transferred_total": 1000,
                "local_copy_percent": 100,
            }
        )
        self.assertTrue(snap["done"])
        self.assertEqual(snap["phase"], "done")
        self.assertEqual(snap["percent"], 100.0)

    def test_error(self) -> None:
        snap = progress_snapshot({"status": "fail", "error": 5})
        self.assertEqual(snap["phase"], "error")
        self.assertTrue(snap["error"])

    def test_task_ids(self) -> None:
        self.assertEqual(extract_task_ids({"status": "success", "task_id": 7}), [7])
        self.assertEqual(extract_task_ids({"task": {"id": 4, "task_id": 9}}), [9])
        self.assertEqual(set(extract_task_ids({"tasks": [{"id": 3}, {"task_id": 4}]})), {3, 4})
        self.assertEqual(extract_task_ids({"status": "fail"}), [])


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeRpi().start()
        self.client = RpiClient("127.0.0.1", self.fake.port, timeout=3)

    def tearDown(self) -> None:
        self.fake.stop()

    def test_install_and_progress(self) -> None:
        response = self.client.install(["http://192.168.1.20/Game.pkg"])
        self.assertEqual(response["task_id"], 11)
        self.assertEqual(
            self.fake.payloads_for("/api/install")[0],
            {"type": "direct", "packages": ["http://192.168.1.20/Game.pkg"]},
        )
        snaps = []
        for _ in range(5):
            snaps.append(progress_snapshot(self.client.progress(11)))
        self.assertEqual(snaps[0]["percent"], 25.0)
        self.assertEqual(snaps[-1]["phase"], "done")
        self.assertTrue(snaps[-1]["done"])

    def test_pause_resume(self) -> None:
        self.client.pause(11)
        self.assertTrue(self.fake.paused)
        self.client.resume(11)
        self.assertFalse(self.fake.paused)
        self.assertEqual(
            self.fake.payloads_for("/api/pause_task"), [{"task_id": 11}]
        )
        self.assertEqual(
            self.fake.payloads_for("/api/resume_task"), [{"task_id": 11}]
        )

    def test_stop(self) -> None:
        self.client.stop(11)
        self.assertTrue(self.fake.stopped)
        snap = progress_snapshot(self.client.progress(11))
        self.assertEqual(snap["phase"], "error")

    def test_is_exists(self) -> None:
        self.assertTrue(self.client.is_exists("CUSA00000")["exists"])

    def test_install_failure(self) -> None:
        self.fake.install_status = "fail"
        with self.assertRaises(RpiError):
            self.client.install(["http://x/a.pkg"])

    def test_connection_failure(self) -> None:
        client = RpiClient("127.0.0.1", 1, timeout=0.2)
        with self.assertRaises(RpiError):
            client.install(["http://x/a.pkg"])

    def test_headers(self) -> None:
        self.client.install(["http://x/a.pkg"])
        headers = self.fake.requests[0]["headers"]
        self.assertEqual(headers["content-type"], "application/json")

    def test_health(self) -> None:
        self.assertTrue(self.client.health())
        self.assertFalse(RpiClient("127.0.0.1", 1, timeout=0.1).health())


if __name__ == "__main__":
    unittest.main()
