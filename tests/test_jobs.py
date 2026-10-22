from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from ps4web.config import Config
from ps4web.consoles import ConsoleStore
from ps4web.jobs import (
    JobError,
    JobManager,
    JobStore,
    parse_7z_listing,
)
from ps4web.library import Library
from tests.fake_rpi import FakeRpi
from tests.pkg_fixture import basic_fields, build_pkg

STUB_7Z = r'''#!/usr/bin/env python3
import os
import sys
import time

args = sys.argv[1:]
if args and args[0] == "l":
    archive = args[-1]
    total = int(os.environ.get("FAKE_7Z_TOTAL", "5"))
    print("7-Zip listing")
    print("--")
    print("Path = " + archive)
    print("Type = Rar")
    print("Physical Size = 1")
    print("----------")
    print("Path = TestGame/")
    print("Folder = +")
    print("Size = 0")
    print("")
    print("Path = TestGame/game.pkg")
    print("Size = " + str(total))
    print("Folder = -")
    raise SystemExit(0)

dest = "."
for arg in args:
    if arg.startswith("-o"):
        dest = arg[2:]
    if arg == "-o":
        raise SystemExit(2)
os.makedirs(os.path.join(dest, "TestGame"), exist_ok=True)
with open(os.path.join(dest, "TestGame", "game.pkg"), "wb") as fh:
    fh.write(b"stub pkg data")
if os.environ.get("FAKE_7Z_BACKSPACE"):
    # 7z updates its percentage with backspaces and does not emit a newline
    # until the extraction finishes.
    sys.stdout.write("  0%")
    sys.stdout.flush()
    time.sleep(0.3)
    sys.stdout.write("\b\b\b\b 10%")
    sys.stdout.flush()
    time.sleep(float(os.environ.get("FAKE_7Z_SLEEP", "0")))
    sys.stdout.write("\b\b\b\b100%\n")
else:
    print(" 10% - TestGame/game.pkg")
    sys.stdout.flush()
    time.sleep(float(os.environ.get("FAKE_7Z_SLEEP", "0")))
    print("100% - TestGame/game.pkg")
'''


def wait_until(predicate, timeout: float = 6.0, interval: float = 0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("timeout waiting for condition")


class ListingTests(unittest.TestCase):
    def test_parse(self) -> None:
        text = """7-Zip
Path = /tmp/a.rar
Type = Rar
Physical Size = 12
----------
Path = Folder/
Folder = +
Size = 0

Path = Folder/a.pkg
Size = 100
Folder = -
"""
        entries = parse_7z_listing(text)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["path"], "/tmp/a.rar")
        self.assertEqual(entries[0]["type"], "Rar")
        self.assertTrue(entries[1]["is_dir"])
        self.assertEqual(entries[2]["size"], 100)


class JobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "library"
        self.root.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.cache = Path(self.tmp.name) / "cache"
        self.config = Config(
            library_root=self.root,
            public_base_url="http://192.168.1.20",
            state_dir=self.state,
            cache_dir=self.cache,
        )
        self.library = Library(self.root, self.cache)
        self.consoles = ConsoleStore(self.state)
        self.fake = FakeRpi().start()
        self.console = self.consoles.add(
            "Test", "127.0.0.1", rpi_port=self.fake.port, firmware="9.00"
        )
        self.stub = Path(self.tmp.name) / "7z-stub"
        self.stub.write_text(STUB_7Z)
        self.stub.chmod(0o755)
        self.managers: list[JobManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.shutdown()
        self.fake.stop()
        self.tmp.cleanup()

    def manager(self, **kwargs) -> JobManager:
        defaults = dict(
            poll_interval=0.02,
            finish_grace=0.2,
            done_grace=0.05,
            stall_timeout=2.0,
            seven_zip=str(self.stub),
        )
        defaults.update(kwargs)
        manager = JobManager(
            self.config, self.consoles, self.library, **defaults
        )
        self.managers.append(manager)
        return manager

    def _pkg(self, name: str, **fields) -> Path:
        return build_pkg(self.root / name, basic_fields(**fields))

    def test_install_batch_sorted_and_completed(self) -> None:
        base = self._pkg("base.pkg")
        patch = self._pkg("patch.pkg", VERSION="01.31", CATEGORY="gp")
        dlc = self._pkg(
            "dlc.pkg", VERSION="01.00", CATEGORY="ac", TITLE_ID="CUSA00001"
        )
        manager = self.manager()
        jobs, warnings = manager.create_install_batch(str(self.console.id), ["."])
        self.assertEqual(warnings, [])
        self.assertEqual(len(jobs), 2)  # DLC excluded by default
        self.assertEqual([job.package_path for job in jobs], ["base.pkg", "patch.pkg"])
        for job in jobs:
            wait_until(lambda: manager.get(job.id).state == "done")
        self.assertEqual(len(self.fake.payloads_for("/api/install")), 2)
        self.assertEqual(
            self.fake.packages,
            [
                "http://192.168.1.20/" + jobs[-1].package_path,
            ],
        )
        self.assertTrue(manager.get(jobs[0].id).url.endswith("/base.pkg"))
        self.assertTrue(manager.get(jobs[1].id).url.endswith("/patch.pkg"))
        self.assertEqual(manager.get(jobs[0].id).progress, 100.0)

    def test_stale_done_progress_is_not_trusted(self) -> None:
        self._pkg("base.pkg")
        self.fake.progress_overrides = [
            {
                "status": "success",
                "error": 0,
                "length": 0,
                "transferred": 0,
                "length_total": 0,
                "transferred_total": 0,
                "local_copy_percent": 100,
                "preparing_percent": 2,
            },
            {
                "status": "success",
                "error": 0x80990079,
                "length": 0,
                "transferred": 0,
                "length_total": 0,
                "transferred_total": 0,
                "local_copy_percent": 0,
                "preparing_percent": 2,
            },
        ]
        manager = self.manager(poll_interval=0.05, done_grace=0.2)
        jobs, _ = manager.create_install_batch(str(self.console.id), ["base.pkg"])
        wait_until(lambda: manager.get(jobs[0].id).state == "error", timeout=3)
        self.assertIn("RPI error", manager.get(jobs[0].id).error)
    def test_include_dlc_and_duplicate_skip(self) -> None:
        self._pkg("base.pkg")
        self._pkg("patch.pkg", VERSION="01.31", CATEGORY="gp")
        self._pkg("dlc.pkg", VERSION="01.00", CATEGORY="ac", TITLE_ID="CUSA00001")
        manager = self.manager(start_workers=False)
        jobs, _ = manager.create_install_batch(
            str(self.console.id), ["."], include_dlc=True
        )
        self.assertEqual(len(jobs), 3)
        again, warnings = manager.create_install_batch(str(self.console.id), ["."])
        self.assertEqual(again, [])
        self.assertTrue(any("Ya está en cola" in warning for warning in warnings))

    def test_firmware_warning(self) -> None:
        self._pkg("base.pkg")
        manager = self.manager(start_workers=False)
        jobs, _ = manager.create_install_batch(str(self.console.id), ["base.pkg"])
        self.assertEqual(len(jobs), 1)
        self.assertTrue(any("Requiere firmware 10.00" in item for item in jobs[0].warnings))

    def test_pause_resume(self) -> None:
        self._pkg("base.pkg")
        self.fake.total = 100000
        manager = self.manager(poll_interval=0.15, finish_grace=0.1)
        jobs, _ = manager.create_install_batch(str(self.console.id), ["base.pkg"])
        job_id = jobs[0].id
        wait_until(lambda: manager.get(job_id).state == "running")
        manager.pause(job_id)
        wait_until(lambda: manager.get(job_id).state == "paused")
        wait_until(lambda: "/api/pause_task" in self.fake.request_paths())
        progress = manager.get(job_id).progress
        time.sleep(0.3)
        self.assertEqual(manager.get(job_id).progress, progress)
        manager.resume(job_id)
        wait_until(lambda: "/api/resume_task" in self.fake.request_paths())
        self.fake.total = 500
        wait_until(lambda: manager.get(job_id).state == "done", timeout=8)

    def test_cancel(self) -> None:
        self._pkg("base.pkg")
        self.fake.total = 100000
        manager = self.manager(poll_interval=0.1)
        jobs, _ = manager.create_install_batch(str(self.console.id), ["base.pkg"])
        job_id = jobs[0].id
        wait_until(lambda: manager.get(job_id).state == "running")
        manager.cancel(job_id)
        wait_until(lambda: "/api/stop_task" in self.fake.request_paths())
        self.assertEqual(manager.get(job_id).state, "cancelled")

    def test_retry(self) -> None:
        self._pkg("base.pkg")
        manager = self.manager(start_workers=False)
        jobs, _ = manager.create_install_batch(str(self.console.id), ["base.pkg"])
        job = jobs[0]
        with manager._lock:
            job.state = "error"
            job.error = "boom"
            manager._persist_locked()
        manager.retry(job.id)
        self.assertEqual(manager.get(job.id).state, "queued")
        self.assertIsNone(manager.get(job.id).error)

    def test_clear_terminal_keeps_active_jobs(self) -> None:
        self._pkg("base.pkg")
        self._pkg("patch.pkg", VERSION="01.31", CATEGORY="gp")
        self._pkg("dlc.pkg", VERSION="01.00", CATEGORY="ac", TITLE_ID="CUSA00001")
        archive = self.root / "game.rar"
        archive.write_bytes(b"rar")
        manager = self.manager(start_workers=False)
        jobs, _ = manager.create_install_batch(
            str(self.console.id), ["base.pkg", "patch.pkg", "dlc.pkg"], include_dlc=True
        )
        finished, failed, active = jobs[0], jobs[1], jobs[2]
        cancelled = manager.create_extract("game.rar")
        with manager._lock:
            finished.state = "done"
            failed.state = "error"
            cancelled.state = "cancelled"
            manager._persist_locked()

        removed = manager.clear_terminal()

        self.assertEqual(removed, 3)
        self.assertEqual([job.id for job in manager.list()], [active.id])
        restarted = self.manager(start_workers=False)
        self.assertEqual([job.id for job in restarted.list()], [active.id])

    def test_clear_terminal_filters_by_console(self) -> None:
        self._pkg("base.pkg")
        other = self.consoles.add(
            "Other",
            "127.0.0.1",
            rpi_port=self.fake.port + 1,
            firmware="9.00",
        )
        manager = self.manager(start_workers=False)
        first_jobs, _ = manager.create_install_batch(
            str(self.console.id), ["base.pkg"]
        )
        second_jobs, _ = manager.create_install_batch(str(other.id), ["base.pkg"])
        with manager._lock:
            first_jobs[0].state = "done"
            second_jobs[0].state = "error"
            manager._persist_locked()

        removed = manager.clear_terminal(str(self.console.id))

        self.assertEqual(removed, 1)
        self.assertEqual([job.id for job in manager.list()], [second_jobs[0].id])

    def test_extract_done(self) -> None:
        archive = self.root / "game.rar"
        archive.write_bytes(b"rar")
        manager = self.manager()
        job = manager.create_extract("game.rar")
        wait_until(lambda: manager.get(job.id).state == "done", timeout=8)
        self.assertTrue((self.root / "TestGame" / "game.pkg").is_file())
        self.assertEqual(manager.get(job.id).progress, 100.0)

    def test_extract_progress_with_backspace_output(self) -> None:
        archive = self.root / "game.rar"
        archive.write_bytes(b"rar")
        old_backspace = os.environ.get("FAKE_7Z_BACKSPACE")
        old_sleep = os.environ.get("FAKE_7Z_SLEEP")
        os.environ["FAKE_7Z_BACKSPACE"] = "1"
        os.environ["FAKE_7Z_SLEEP"] = "2"
        try:
            manager = self.manager()
            job = manager.create_extract("game.rar")
            wait_until(lambda: manager.get(job.id).progress >= 10, timeout=1.2)
            wait_until(lambda: manager.get(job.id).state == "done", timeout=4)
        finally:
            if old_backspace is None:
                os.environ.pop("FAKE_7Z_BACKSPACE", None)
            else:
                os.environ["FAKE_7Z_BACKSPACE"] = old_backspace
            if old_sleep is None:
                os.environ.pop("FAKE_7Z_SLEEP", None)
            else:
                os.environ["FAKE_7Z_SLEEP"] = old_sleep

    def test_extract_existing_requires_overwrite(self) -> None:
        archive = self.root / "game.rar"
        archive.write_bytes(b"rar")
        (self.root / "TestGame").mkdir()
        manager = self.manager()
        job = manager.create_extract("game.rar", overwrite=False)
        wait_until(lambda: manager.get(job.id).state == "error")
        self.assertIn("Ya existe", manager.get(job.id).error or "")
        job2 = manager.create_extract("game.rar", overwrite=True)
        wait_until(lambda: manager.get(job2.id).state == "done", timeout=8)

    def test_extract_delete_archive(self) -> None:
        archive = self.root / "game.rar"
        archive.write_bytes(b"rar")
        manager = self.manager()
        job = manager.create_extract("game.rar", delete_archive=True)
        wait_until(lambda: manager.get(job.id).state == "done", timeout=8)
        self.assertFalse(archive.exists())
        self.assertTrue((self.root / "TestGame" / "game.pkg").is_file())

    def test_extract_cancel(self) -> None:
        archive = self.root / "game.rar"
        archive.write_bytes(b"rar")
        manager = self.manager()
        old = os.environ.get("FAKE_7Z_SLEEP")
        os.environ["FAKE_7Z_SLEEP"] = "5"
        try:
            job = manager.create_extract("game.rar")
            wait_until(lambda: manager.get(job.id).state == "extracting")
            manager.cancel(job.id)
            wait_until(lambda: manager.get(job.id).state == "cancelled", timeout=3)
        finally:
            if old is None:
                os.environ.pop("FAKE_7Z_SLEEP", None)
            else:
                os.environ["FAKE_7Z_SLEEP"] = old

    def test_restart_marks_running_as_error(self) -> None:
        self._pkg("base.pkg")
        manager = self.manager(start_workers=False)
        jobs, _ = manager.create_install_batch(str(self.console.id), ["base.pkg"])
        with manager._lock:
            jobs[0].state = "running"
            manager._persist_locked()
        restarted = self.manager(start_workers=False)
        job = restarted.get(jobs[0].id)
        self.assertEqual(job.state, "error")
        self.assertIn("Interrumpido", job.error or "")

    def test_invalid_operations(self) -> None:
        self._pkg("base.pkg")
        manager = self.manager(start_workers=False)
        jobs, _ = manager.create_install_batch(str(self.console.id), ["base.pkg"])
        with self.assertRaises(JobError):
            manager.resume(jobs[0].id)
        with self.assertRaises(JobError):
            manager.cancel(jobs[0].id) if False else manager.pause(jobs[0].id)
        with self.assertRaises(JobError):
            manager.create_install_batch("missing", ["base.pkg"])

    def test_job_store_round_trip(self) -> None:
        store = JobStore(self.state)
        job = self._make_job()
        store.save([job])
        loaded = JobStore(self.state).load()
        self.assertEqual(loaded[0].id, job.id)
        self.assertEqual(loaded[0].warnings, ["w"])

    def _make_job(self):
        from ps4web.jobs import Job

        return Job(
            id="abc",
            type="install",
            title="X",
            console_name="PS4",
            warnings=["w"],
        )


if __name__ == "__main__":
    unittest.main()
