"""Configuration and password helpers for ps4web."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

APP_VERSION = "1.0.0"
DEFAULT_CONFIG_PATH = "/etc/ps4web/config.json"
DEFAULT_LIBRARY_ROOT = "/mnt/juegos_ps4/descargas"
DEFAULT_PUBLIC_BASE_URL = "http://192.168.1.20"
DEFAULT_STATE_DIR = "/var/lib/ps4web"
DEFAULT_RPI_PKG = (
    "/mnt/juegos_ps4/descargas/_instaladores/"
    "Remote_Package_Installer_FLTZ00003.pkg"
)
DEFAULT_SCAN_PORTS = (12800, 12801, 2121, 9295)
PBKDF2_ITERATIONS = 200_000


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or invalid."""


def _as_path(value: Any, name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ConfigError(f"Config value '{name}' must be a non-empty path")
    return Path(os.path.expanduser(str(value)))


def normalize_base_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ConfigError("public_base_url cannot be empty")
    if "://" not in value:
        value = "http://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"Invalid public_base_url: {value!r}")
    return value.rstrip("/")


@dataclass
class Config:
    library_root: Path = Path(DEFAULT_LIBRARY_ROOT)
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL
    listen_host: str = "0.0.0.0"
    listen_port: int = 8081
    subnet: str = "192.168.1.0/24"
    state_dir: Path = Path(DEFAULT_STATE_DIR)
    cache_dir: Path | None = None
    auth_user: str = "admin"
    auth_password_hash: str = ""
    session_secret: str = ""
    rpi_pkg: Path = Path(DEFAULT_RPI_PKG)
    scan_ports: tuple[int, ...] = DEFAULT_SCAN_PORTS
    app_version: str = APP_VERSION
    _path: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.library_root = _as_path(self.library_root, "library_root")
        self.rpi_pkg = _as_path(self.rpi_pkg, "rpi_pkg")
        self.state_dir = _as_path(self.state_dir, "state_dir")
        if self.cache_dir is None:
            self.cache_dir = self.state_dir / "cache"
        self.cache_dir = _as_path(self.cache_dir, "cache_dir")
        self.public_base_url = normalize_base_url(self.public_base_url)
        if not (1 <= int(self.listen_port) <= 65535):
            raise ConfigError("listen_port must be between 1 and 65535")
        self.listen_port = int(self.listen_port)
        if not self.auth_user:
            self.auth_user = "admin"
        try:
            self.scan_ports = tuple(int(p) for p in self.scan_ports)
        except (TypeError, ValueError) as exc:
            raise ConfigError("scan_ports must be integers") from exc
        for port in self.scan_ports:
            if not (1 <= port <= 65535):
                raise ConfigError(f"Invalid scan port: {port}")
        if self._path is not None:
            self._path = Path(self._path)

    @property
    def consoles_path(self) -> Path:
        return self.state_dir / "consoles.json"

    @property
    def jobs_path(self) -> Path:
        return self.state_dir / "jobs.json"

    @property
    def icons_dir(self) -> Path:
        return self.cache_dir / "icons"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.icons_dir.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "library_root": str(self.library_root),
            "public_base_url": self.public_base_url,
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
            "subnet": self.subnet,
            "state_dir": str(self.state_dir),
            "cache_dir": str(self.cache_dir),
            "auth_user": self.auth_user,
            "auth_password_hash": self.auth_password_hash,
            "session_secret": self.session_secret,
            "rpi_pkg": str(self.rpi_pkg),
            "scan_ports": list(self.scan_ports),
            "app_version": self.app_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> "Config":
        if not isinstance(data, dict):
            raise ConfigError("Config root must be a JSON object")
        known = {
            "library_root",
            "public_base_url",
            "listen_host",
            "listen_port",
            "subnet",
            "state_dir",
            "cache_dir",
            "auth_user",
            "auth_password_hash",
            "session_secret",
            "rpi_pkg",
            "scan_ports",
            "app_version",
        }
        kwargs = {k: v for k, v in data.items() if k in known}
        try:
            return cls(**kwargs, _path=path)
        except TypeError as exc:
            raise ConfigError(f"Invalid config: {exc}") from exc


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    env_path = os.environ.get("PS4WEB_CONFIG")
    config_path = Path(path or env_path or DEFAULT_CONFIG_PATH)
    if not config_path.is_file():
        raise ConfigError(
            f"Config file not found: {config_path}. "
            "Run `python -m ps4web init-config` to create it."
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read config {config_path}: {exc}") from exc
    config = Config.from_dict(raw, path=config_path)
    if not config.auth_password_hash:
        raise ConfigError("auth_password_hash is empty; set a password first")
    if not config.session_secret:
        raise ConfigError("session_secret is empty; regenerate the config")
    return config


def save_config(config: Config, path: str | os.PathLike[str] | None = None) -> Path:
    target = Path(path or config._path or DEFAULT_CONFIG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    config._path = target
    return target


def hash_password(password: str, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("Password cannot be empty")
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return (
        f"pbkdf2_sha256${PBKDF2_ITERATIONS}$"
        f"{salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, stored: str) -> bool:
    if not password or not stored:
        return False
    try:
        algorithm, iterations_s, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_s)
        if iterations < 1 or iterations > 2_000_000:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(actual, expected)


def generate_password(length: int = 18) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def init_config(
    path: str | os.PathLike[str],
    library_root: str = DEFAULT_LIBRARY_ROOT,
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL,
    password: str | None = None,
    listen_host: str = "0.0.0.0",
    listen_port: int = 8081,
    subnet: str = "192.168.1.0/24",
    state_dir: str = DEFAULT_STATE_DIR,
    rpi_pkg: str = DEFAULT_RPI_PKG,
    auth_user: str = "admin",
    force: bool = False,
) -> tuple[Config, str]:
    target = Path(path)
    if target.exists() and not force:
        raise ConfigError(f"Config already exists: {target} (use force to replace)")
    plaintext = password or generate_password()
    config = Config(
        library_root=Path(library_root),
        public_base_url=public_base_url,
        listen_host=listen_host,
        listen_port=listen_port,
        subnet=subnet,
        state_dir=Path(state_dir),
        rpi_pkg=Path(rpi_pkg),
        auth_user=auth_user,
        auth_password_hash=hash_password(plaintext),
        session_secret=secrets.token_hex(32),
        _path=target,
    )
    config.ensure_dirs()
    save_config(config, target)
    return config, plaintext


def public_config(config: Config) -> dict[str, Any]:
    return {
        "library_root": str(config.library_root),
        "public_base_url": config.public_base_url,
        "listen_host": config.listen_host,
        "listen_port": config.listen_port,
        "subnet": config.subnet,
        "rpi_pkg": str(config.rpi_pkg),
        "scan_ports": list(config.scan_ports),
        "app_version": config.app_version,
    }
