from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from ps4web.config import (
    Config,
    ConfigError,
    generate_password,
    hash_password,
    init_config,
    load_config,
    normalize_base_url,
    public_config,
    save_config,
    verify_password,
)


class PasswordTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        stored = hash_password("s3cret!")
        self.assertTrue(verify_password("s3cret!", stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertFalse(verify_password("", stored))

    def test_salt_is_random(self) -> None:
        self.assertNotEqual(hash_password("same"), hash_password("same"))

    def test_malformed_hash(self) -> None:
        for value in ("", "nope", "pbkdf2_sha256$x$y$z", "md5$1$aa$bb"):
            self.assertFalse(verify_password("x", value))

    def test_generated_password_shape(self) -> None:
        password = generate_password()
        self.assertGreaterEqual(len(password), 16)
        self.assertNotIn("0", password)
        self.assertNotIn("O", password)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = self.root / "library"
        self.library.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_init_and_load(self) -> None:
        path = self.root / "etc" / "config.json"
        config, password = init_config(
            path,
            library_root=str(self.library),
            public_base_url="192.168.1.20:8081/",
            state_dir=str(self.root / "state"),
        )
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        loaded = load_config(path)
        self.assertEqual(loaded.library_root, self.library)
        self.assertEqual(loaded.public_base_url, "http://192.168.1.20:8081")
        self.assertEqual(loaded.state_dir, self.root / "state")
        self.assertTrue(verify_password(password, loaded.auth_password_hash))
        self.assertTrue(loaded.session_secret)
        self.assertEqual(loaded.consoles_path, self.root / "state" / "consoles.json")

    def test_init_refuses_overwrite(self) -> None:
        path = self.root / "config.json"
        init_config(path, library_root=str(self.library), state_dir=str(self.root / "s"))
        with self.assertRaises(ConfigError):
            init_config(path, library_root=str(self.library), state_dir=str(self.root / "s"))

    def test_missing_config(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.root / "missing.json")

    def test_bad_base_url(self) -> None:
        with self.assertRaises(ConfigError):
            normalize_base_url("ftp://host")
        with self.assertRaises(ConfigError):
            normalize_base_url("")

    def test_save_round_trip_and_public_config(self) -> None:
        path = self.root / "config.json"
        config = Config(
            library_root=self.library,
            public_base_url="http://10.0.0.2",
            state_dir=self.root / "state",
            auth_password_hash=hash_password("pw"),
            session_secret="x" * 64,
        )
        save_config(config, path)
        raw = json.loads(path.read_text())
        self.assertEqual(raw["library_root"], str(self.library))
        self.assertNotIn("auth_password_hash", public_config(config))
        self.assertEqual(public_config(config)["app_version"], "1.0.0")

    def test_invalid_port(self) -> None:
        with self.assertRaises(ConfigError):
            Config(listen_port=0)
        with self.assertRaises(ConfigError):
            Config(listen_port=70000)
        with self.assertRaises(ConfigError):
            Config(scan_ports=(0, 1))

    def test_empty_password_hash_rejected_on_load(self) -> None:
        path = self.root / "config.json"
        path.write_text(
            json.dumps(
                {
                    "library_root": str(self.library),
                    "state_dir": str(self.root / "state"),
                    "session_secret": "abc",
                    "auth_password_hash": "",
                }
            )
        )
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_state_dirs_created(self) -> None:
        config = Config(
            library_root=self.library,
            state_dir=self.root / "state",
            auth_password_hash=hash_password("x"),
            session_secret="s",
        )
        config.ensure_dirs()
        self.assertTrue(config.icons_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
