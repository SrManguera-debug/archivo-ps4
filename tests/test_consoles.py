from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ps4web.consoles import (
    Console,
    ConsoleError,
    ConsoleStore,
    check_ports,
    discover,
    prepare_rpi,
    probe,
    validate_console,
    validate_ip,
    validate_port,
)


class Listener:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, 0))
        self.sock.listen(5)
        self.host, self.port = self.sock.getsockname()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except OSError:
                return

    def close(self) -> None:
        self.sock.close()


class ConsoleValidationTests(unittest.TestCase):
    def test_ip_and_port(self) -> None:
        self.assertEqual(validate_ip(" 192.168.1.50 "), "192.168.1.50")
        for value in ("", "999.1.1.1", "::1", "0.0.0.0", "224.0.0.1", "host"):
            with self.assertRaises(ConsoleError, msg=value):
                validate_ip(value)
        self.assertEqual(validate_port("12800"), 12800)
        for value in (0, 65536, "x"):
            with self.assertRaises(ConsoleError):
                validate_port(value)

    def test_console_rules(self) -> None:
        console = Console.create(" PS4 ", "192.168.1.50", firmware="9.00")
        validate_console(console)
        self.assertEqual(console.name, "PS4")
        with self.assertRaises(ConsoleError):
            validate_console(Console.create("", "192.168.1.50"))
        with self.assertRaises(ConsoleError):
            validate_console(Console.create("PS4", "192.168.1.50", firmware="nope"))
        with self.assertRaises(ConsoleError):
            validate_console(Console.create("PS4", "192.168.1.50", rpi_port=0))


class ConsoleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.store = ConsoleStore(self.state)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_crud_and_persistence(self) -> None:
        console = self.store.add("Salón", "192.168.1.50", firmware="9.00")
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.get(console.id).name, "Salón")
        updated = self.store.update(console.id, name="Oficina", notes="junto al TV")
        self.assertEqual(updated.name, "Oficina")
        self.assertEqual(updated.notes, "junto al TV")
        self.assertEqual(updated.created_at, console.created_at)
        reopened = ConsoleStore(self.state)
        self.assertEqual(reopened.list()[0].name, "Oficina")
        reopened.delete(console.id)
        self.assertEqual(reopened.list(), [])
        self.assertEqual(ConsoleStore(self.state).list(), [])

    def test_duplicate_and_missing(self) -> None:
        self.store.add("A", "192.168.1.50")
        with self.assertRaises(ConsoleError):
            self.store.add("B", "192.168.1.50")
        with self.assertRaises(ConsoleError):
            self.store.require("missing")
        with self.assertRaises(ConsoleError):
            self.store.delete("missing")

    def test_update_duplicate(self) -> None:
        first = self.store.add("A", "192.168.1.50")
        self.store.add("B", "192.168.1.51")
        with self.assertRaises(ConsoleError):
            self.store.update(first.id, ip="192.168.1.51")

    def test_corrupt_file_is_backed_up(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "consoles.json").write_text("{not json")
        store = ConsoleStore(self.state)
        self.assertEqual(store.list(), [])
        self.assertTrue(list(self.state.glob("consoles.corrupt-*")))

    def test_file_permissions(self) -> None:
        self.store.add("A", "192.168.1.50")
        self.assertEqual((self.state / "consoles.json").stat().st_mode & 0o777, 0o600)


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rpi = Listener()
        self.ftp = Listener()

    def tearDown(self) -> None:
        self.rpi.close()
        self.ftp.close()

    def test_check_ports(self) -> None:
        result = check_ports("127.0.0.1", [self.rpi.port, self.ftp.port, 1], timeout=0.2)
        self.assertTrue(result[self.rpi.port])
        self.assertTrue(result[self.ftp.port])
        self.assertFalse(result[1])

    def test_probe(self) -> None:
        console = Console.create(
            "Test", "127.0.0.1", rpi_port=self.rpi.port, ftp_port=self.ftp.port
        )
        status = probe(console, timeout=0.2)
        self.assertTrue(status["rpi"])
        self.assertEqual(status["rpi_port"], self.rpi.port)
        self.assertTrue(status["ftp"])
        self.assertTrue(status["online"])
        self.assertIn(self.rpi.port, status["open_ports"])

    def test_discover_local(self) -> None:
        listener = Listener("127.0.0.2")
        try:
            found = discover(
                "127.0.0.0/29",
                ports=(listener.port,),
                timeout=0.1,
                max_workers=8,
            )
            self.assertTrue(any(item["ip"] == "127.0.0.2" for item in found))
        finally:
            listener.close()

    def test_discover_rejects_huge(self) -> None:
        with self.assertRaises(ConsoleError):
            discover("10.0.0.0/8")


class PrepareRpiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.pkg = Path(self.tmp.name) / "rpi.pkg"
        self.pkg.write_bytes(b"\x7fCNT" + b"x" * 100)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_upload(self) -> None:
        calls: dict[str, object] = {}

        class FakeFTP:
            def connect(self, host, port, timeout=10):
                calls["connect"] = (host, port, timeout)
                return self

            def login(self, user, password):
                calls["login"] = (user, password)

            def set_pasv(self, value):
                calls["pasv"] = value

            def cwd(self, path):
                calls["cwd"] = path
                return "250 ok"

            def storbinary(self, cmd, fh, blocksize=8192):
                calls["stor"] = (cmd, len(fh.read()), blocksize)

            def quit(self):
                calls["quit"] = True

        with mock.patch("ps4web.consoles.ftplib.FTP", FakeFTP):
            console = Console.create("PS4", "192.168.1.50", ftp_port=2121)
            remote = prepare_rpi(console, self.pkg)
        self.assertEqual(remote, "/data/pkg/rpi.pkg")
        self.assertEqual(calls["connect"], ("192.168.1.50", 2121, 10.0))
        self.assertEqual(calls["login"], ("anonymous", "anonymous"))
        self.assertEqual(calls["stor"][0], "STOR rpi.pkg")
        self.assertTrue(calls["quit"])

    def test_missing_pkg(self) -> None:
        console = Console.create("PS4", "192.168.1.50")
        with self.assertRaises(ConsoleError):
            prepare_rpi(console, Path(self.tmp.name) / "missing.pkg")


if __name__ == "__main__":
    unittest.main()
