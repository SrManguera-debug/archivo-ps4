"""Session and login helpers."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

from .config import Config, verify_password

SESSION_COOKIE = "ps4web_session"
SESSION_TTL = 7 * 24 * 60 * 60
CSRF_HEADER = "X-CSRF-Token"


@dataclass
class Session:
    token: str
    user: str
    csrf: str
    created_at: float
    last_seen: float = field(default_factory=time.time)


class AuthManager:
    """In-memory sessions plus constant-time password verification.

    Sessions intentionally do not survive a restart. The app is a LAN tool and
    a restart is a good moment to force a fresh login.
    """

    def __init__(self, config: Config, ttl: int = SESSION_TTL) -> None:
        self.config = config
        self.ttl = ttl
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    def login(self, username: str, password: str) -> Session | None:
        user_ok = hmac.compare_digest(
            (username or "").strip(), self.config.auth_user
        )
        pass_ok = verify_password(password or "", self.config.auth_password_hash)
        if not (user_ok and pass_ok):
            return None
        session = Session(
            token=secrets.token_urlsafe(32),
            user=username.strip(),
            csrf=secrets.token_urlsafe(24),
            created_at=time.time(),
        )
        with self._lock:
            self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            if now - session.created_at > self.ttl:
                self._sessions.pop(token, None)
                return None
            session.last_seen = now
            return session

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def check_csrf(self, session: Session | None, token: str | None) -> bool:
        if session is None or not token:
            return False
        return hmac.compare_digest(session.csrf, token)

    def purge_expired(self) -> int:
        cutoff = time.time() - self.ttl
        with self._lock:
            expired = [
                token
                for token, session in self._sessions.items()
                if session.created_at < cutoff
            ]
            for token in expired:
                self._sessions.pop(token, None)
        return len(expired)

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)
