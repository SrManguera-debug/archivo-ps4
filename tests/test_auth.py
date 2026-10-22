from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ps4web.auth import AuthManager
from ps4web.config import Config, hash_password


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(
            library_root=Path(self.tmp.name),
            state_dir=Path(self.tmp.name) / "state",
            auth_user="admin",
            auth_password_hash=hash_password("correct horse"),
            session_secret="s" * 64,
        )
        self.auth = AuthManager(self.config, ttl=60)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_login_success(self) -> None:
        session = self.auth.login("admin", "correct horse")
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.user, "admin")
        self.assertEqual(self.auth.get(session.token), session)

    def test_login_failure(self) -> None:
        self.assertIsNone(self.auth.login("admin", "wrong"))
        self.assertIsNone(self.auth.login("root", "correct horse"))
        self.assertIsNone(self.auth.login("", ""))

    def test_logout(self) -> None:
        session = self.auth.login("admin", "correct horse")
        assert session is not None
        self.auth.logout(session.token)
        self.assertIsNone(self.auth.get(session.token))

    def test_csrf(self) -> None:
        session = self.auth.login("admin", "correct horse")
        assert session is not None
        self.assertTrue(self.auth.check_csrf(session, session.csrf))
        self.assertFalse(self.auth.check_csrf(session, "nope"))
        self.assertFalse(self.auth.check_csrf(None, session.csrf))
        self.assertFalse(self.auth.check_csrf(session, None))

    def test_expiry(self) -> None:
        session = self.auth.login("admin", "correct horse")
        assert session is not None
        session.created_at -= 120
        self.assertIsNone(self.auth.get(session.token))
        self.assertEqual(self.auth.session_count(), 0)

    def test_purge(self) -> None:
        session = self.auth.login("admin", "correct horse")
        assert session is not None
        session.created_at -= 120
        self.assertEqual(self.auth.purge_expired(), 1)

    def test_unknown_token(self) -> None:
        self.assertIsNone(self.auth.get("nope"))
        self.assertIsNone(self.auth.get(None))


if __name__ == "__main__":
    unittest.main()
