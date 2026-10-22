"""Threaded HTTP server, JSON API, authentication and static files."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
import traceback
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .auth import CSRF_HEADER, SESSION_COOKIE, AuthManager, Session
from .config import Config, public_config
from .consoles import (
    ConsoleError,
    ConsoleStore,
    console_public,
    discover,
    prepare_rpi,
    probe,
)
from .jobs import JobError, JobManager
from .library import Library, LibraryError
from .pkg import PkgError, firmware_satisfied, human_size
from .rpi import RpiError

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")
MAX_BODY = 1024 * 1024
STATUS_TTL = 10.0
LOGIN_WINDOW = 60.0
LOGIN_MAX_FAILURES = 6


class App:
    def __init__(self, config: Config, *, start_workers: bool = True) -> None:
        self.config = config
        config.ensure_dirs()
        self.auth = AuthManager(config)
        self.consoles = ConsoleStore(config.state_dir)
        self.library = Library(config.library_root, config.cache_dir)
        self.jobs = JobManager(config, self.consoles, self.library, start_workers=start_workers)
        self._status_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._status_lock = threading.RLock()
        self._login_failures: dict[str, list[float]] = {}
        self._login_lock = threading.RLock()

    def status(self, console_id: str, force: bool = False) -> dict[str, Any]:
        console = self.consoles.require(console_id)
        now = time.time()
        with self._status_lock:
            cached = self._status_cache.get(console_id)
            if not force and cached and now - cached[0] < STATUS_TTL:
                return cached[1]
        value = probe(console)
        with self._status_lock:
            self._status_cache[console_id] = (now, value)
        return value

    def invalidate_status(self, console_id: str) -> None:
        with self._status_lock:
            self._status_cache.pop(console_id, None)

    def login_allowed(self, ip: str) -> bool:
        now = time.time()
        with self._login_lock:
            attempts = [stamp for stamp in self._login_failures.get(ip, []) if now - stamp < LOGIN_WINDOW]
            self._login_failures[ip] = attempts
            return len(attempts) < LOGIN_MAX_FAILURES

    def record_login_failure(self, ip: str) -> None:
        with self._login_lock:
            self._login_failures.setdefault(ip, []).append(time.time())

    def clear_login_failures(self, ip: str) -> None:
        with self._login_lock:
            self._login_failures.pop(ip, None)

    def shutdown(self) -> None:
        self.jobs.shutdown()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ArchivoPS4/1.0"
    sys_version = ""

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # ---------- logging ----------

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    # ---------- response helpers ----------

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; font-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )

    def send_json(self, status: int, data: Any, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_api_error(self, status: int, message: str, code: str | None = None) -> None:
        payload: dict[str, Any] = {"error": message}
        if code:
            payload["code"] = code
        self.send_json(status, payload)

    def send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    # ---------- request helpers ----------

    def _cookies(self) -> SimpleCookie[str]:
        cookies: SimpleCookie[str] = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        return cookies

    def _session(self) -> Session | None:
        morsel = self._cookies().get(SESSION_COOKIE)
        return self.app.auth.get(morsel.value if morsel else None)

    def _require_session(self) -> Session | None:
        session = self._session()
        if session is None:
            self.send_api_error(HTTPStatus.UNAUTHORIZED, "Sesión requerida", "auth")
            return None
        return session

    def _require_csrf(self, session: Session) -> bool:
        token = self.headers.get(CSRF_HEADER)
        if not self.app.auth.check_csrf(session, token):
            self.send_api_error(HTTPStatus.FORBIDDEN, "Token CSRF inválido", "csrf")
            return False
        return True

    def _read_json(self) -> dict[str, Any] | None:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or 0)
        except ValueError:
            self.send_api_error(HTTPStatus.BAD_REQUEST, "Content-Length inválido")
            return None
        if length < 0 or length > MAX_BODY:
            self.send_api_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Cuerpo demasiado grande")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_api_error(HTTPStatus.BAD_REQUEST, "JSON inválido")
            return None
        if not isinstance(data, dict):
            self.send_api_error(HTTPStatus.BAD_REQUEST, "Se esperaba un objeto JSON")
            return None
        return data

    def _query(self) -> dict[str, str]:
        values = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        return {key: items[-1] for key, items in values.items()}

    def _set_session_cookie(self, session: Session) -> None:
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE}={session.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800",
        )

    # ---------- dispatch ----------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            path = urlsplit(self.path).path
            if path.startswith("/static/") or path == "/favicon.ico":
                self._serve_static(path, public=True)
                return
            if path == "/login":
                if method != "GET":
                    self.send_api_error(HTTPStatus.METHOD_NOT_ALLOWED, "Método no permitido")
                    return
                if self._session() is not None:
                    self.redirect("/")
                    return
                self._serve_static("/static/login.html", public=True)
                return
            if path == "/":
                if method != "GET":
                    self.send_api_error(HTTPStatus.METHOD_NOT_ALLOWED, "Método no permitido")
                    return
                if self._session() is None:
                    self.redirect("/login")
                    return
                self._serve_static("/static/index.html", public=True)
                return
            if path == "/health":
                self.send_json(HTTPStatus.OK, {"ok": True, "app": "archivo-ps4"})
                return
            if not path.startswith("/api/"):
                self.send_api_error(HTTPStatus.NOT_FOUND, "No encontrado")
                return
            if method == "POST" and path == "/api/login":
                self._api_login()
                return
            session = self._require_session()
            if session is None:
                return
            if method in ("POST", "PUT", "DELETE"):
                if not self._require_csrf(session):
                    return
            self._route_api(method, path, session)
        except (LibraryError, ConsoleError, JobError, PkgError) as exc:
            self.send_api_error(HTTPStatus.BAD_REQUEST, str(exc))
        except RpiError as exc:
            self.send_api_error(HTTPStatus.BAD_GATEWAY, str(exc), "rpi")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            LOGGER.error("Error no controlado:\n%s", traceback.format_exc())
            try:
                self.send_api_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Error interno")
            except Exception:
                pass

    # ---------- static ----------

    def _serve_static(self, path: str, public: bool = False) -> None:
        rel = path
        if rel == "/favicon.ico":
            body = b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%230070d1'/><text x='16' y='22' font-size='16' text-anchor='middle' fill='white'>PS</text></svg>"
            self.send_bytes(200, body, "image/svg+xml", {"Cache-Control": "public, max-age=86400"})
            return
        if rel.startswith("/static/"):
            rel = rel[len("/static/"):]
        rel = unquote(rel)
        if not rel or rel.startswith("/") or "\x00" in rel or ".." in Path(rel).parts:
            self.send_api_error(HTTPStatus.BAD_REQUEST, "Ruta inválida")
            return
        target = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if os.path.commonpath([str(STATIC_DIR), target]) != str(STATIC_DIR) or not os.path.isfile(target):
            self.send_api_error(HTTPStatus.NOT_FOUND, "No encontrado")
            return
        content_type, _ = mimetypes.guess_type(target)
        content_type = content_type or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json", "image/svg+xml"):
            content_type += "; charset=utf-8"
        try:
            body = Path(target).read_bytes()
        except OSError:
            self.send_api_error(HTTPStatus.NOT_FOUND, "No encontrado")
            return
        cache = "no-cache" if target.endswith((".html", ".js", ".css")) else "public, max-age=3600"
        self.send_bytes(200, body, content_type, {"Cache-Control": cache})

    # ---------- login ----------

    def _api_login(self) -> None:
        ip = self.client_address[0]
        if not self.app.login_allowed(ip):
            self.send_api_error(HTTPStatus.TOO_MANY_REQUESTS, "Demasiados intentos. Esperá un minuto.", "rate_limit")
            return
        data = self._read_json()
        if data is None:
            return
        username = str(data.get("username") or self.app.config.auth_user)
        password = str(data.get("password") or "")
        session = self.app.auth.login(username, password)
        if session is None:
            self.app.record_login_failure(ip)
            self.send_api_error(HTTPStatus.UNAUTHORIZED, "Credenciales incorrectas", "credentials")
            return
        self.app.clear_login_failures(ip)
        # send_json does not know about the cookie, so build the response here.
        body = json.dumps(
            {"user": session.user, "csrf": session.csrf}, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self._set_session_cookie(session)
        self.end_headers()
        self.wfile.write(body)

    # ---------- API routing ----------

    def _route_api(self, method: str, path: str, session: Session) -> None:
        app = self.app
        if path == "/api/me" and method == "GET":
            self.send_json(
                HTTPStatus.OK,
                {
                    "user": session.user,
                    "csrf": session.csrf,
                    "config": public_config(app.config),
                },
            )
            return
        if path == "/api/logout" and method == "POST":
            app.auth.logout(session.token)
            body = b'{"ok":true}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/consoles":
            if method == "GET":
                self._api_consoles_list()
                return
            if method == "POST":
                self._api_console_create()
                return
            self.send_api_error(HTTPStatus.METHOD_NOT_ALLOWED, "Método no permitido")
            return
        if path == "/api/discover" and method == "GET":
            self._api_discover()
            return
        if path == "/api/files" and method == "GET":
            self._api_files()
            return
        if path == "/api/files/delete" and method == "POST":
            self._api_files_delete()
            return
        if path == "/api/pkg" and method == "GET":
            self._api_pkg()
            return
        if path == "/api/icon" and method == "GET":
            self._api_icon()
            return
        if path == "/api/jobs/clear" and method == "POST":
            self._api_jobs_clear()
            return
        if path == "/api/jobs":
            if method == "GET":
                self._api_jobs_list()
                return
            if method == "POST":
                self._api_jobs_create()
                return
            self.send_api_error(HTTPStatus.METHOD_NOT_ALLOWED, "Método no permitido")
            return
        if path == "/api/extract" and method == "POST":
            self._api_extract()
            return
        if path == "/api/stats" and method == "GET":
            self._api_stats()
            return
        match = re.fullmatch(r"/api/consoles/([^/]+)", path)
        if match:
            console_id = unquote(match.group(1))
            if method == "GET":
                self._api_console_get(console_id)
                return
            if method == "PUT":
                self._api_console_update(console_id)
                return
            if method == "DELETE":
                self._api_console_delete(console_id)
                return
            self.send_api_error(HTTPStatus.METHOD_NOT_ALLOWED, "Método no permitido")
            return
        match = re.fullmatch(r"/api/consoles/([^/]+)/status", path)
        if match and method == "GET":
            self._api_console_status(unquote(match.group(1)))
            return
        match = re.fullmatch(r"/api/consoles/([^/]+)/prepare-rpi", path)
        if match and method == "POST":
            self._api_prepare_rpi(unquote(match.group(1)))
            return
        match = re.fullmatch(r"/api/jobs/([^/]+)/(pause|resume|cancel|retry)", path)
        if match and method == "POST":
            self._api_job_action(unquote(match.group(1)), match.group(2))
            return
        self.send_api_error(HTTPStatus.NOT_FOUND, "No encontrado")

    # ---------- API handlers ----------

    def _api_consoles_list(self) -> None:
        data = []
        for console in self.app.consoles.list():
            item = console_public(console)
            item["active_jobs"] = len(self.app.jobs.active_for_console(console.id))
            with self.app._status_lock:
                cached = self.app._status_cache.get(console.id)
            if cached:
                item["status"] = cached[1]
            data.append(item)
        self.send_json(HTTPStatus.OK, {"consoles": data})

    def _api_console_get(self, console_id: str) -> None:
        console = self.app.consoles.require(console_id)
        item = console_public(console)
        item["active_jobs"] = len(self.app.jobs.active_for_console(console.id))
        self.send_json(HTTPStatus.OK, {"console": item})

    def _console_payload(self) -> dict[str, Any] | None:
        data = self._read_json()
        return data

    def _api_console_create(self) -> None:
        data = self._console_payload()
        if data is None:
            return
        console = self.app.consoles.add(
            name=str(data.get("name") or ""),
            ip=str(data.get("ip") or ""),
            rpi_port=data.get("rpi_port", 12800),
            ftp_port=data.get("ftp_port", 2121),
            firmware=str(data.get("firmware") or ""),
            notes=str(data.get("notes") or ""),
        )
        self.send_json(HTTPStatus.CREATED, {"console": console_public(console)})

    def _api_console_update(self, console_id: str) -> None:
        data = self._console_payload()
        if data is None:
            return
        fields = {
            key: data[key]
            for key in ("name", "ip", "rpi_port", "ftp_port", "firmware", "notes")
            if key in data
        }
        console = self.app.consoles.update(console_id, **fields)
        self.app.invalidate_status(console_id)
        self.send_json(HTTPStatus.OK, {"console": console_public(console)})

    def _api_console_delete(self, console_id: str) -> None:
        if self.app.jobs.has_active_jobs(console_id):
            self.send_api_error(
                HTTPStatus.CONFLICT,
                "La consola tiene trabajos activos. Cancelalos antes de eliminarla.",
                "active_jobs",
            )
            return
        self.app.consoles.delete(console_id)
        self.app.invalidate_status(console_id)
        self.send_json(HTTPStatus.OK, {"ok": True})

    def _api_console_status(self, console_id: str) -> None:
        force = self._query().get("force") in ("1", "true", "yes")
        console = self.app.consoles.require(console_id)
        status = self.app.status(console_id, force=force)
        self.send_json(
            HTTPStatus.OK,
            {"console_id": console.id, "name": console.name, "status": status},
        )

    def _api_prepare_rpi(self, console_id: str) -> None:
        console = self.app.consoles.require(console_id)
        remote_path = prepare_rpi(console, self.app.config.rpi_pkg)
        self.app.invalidate_status(console_id)
        self.send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "remote_path": remote_path,
                "message": (
                    "RPI subido por FTP. En la PS4 abrí Ajustes > Ajustes de depuración "
                    "> Juego > Instalar paquete y elegí el archivo."
                ),
            },
        )

    def _api_discover(self) -> None:
        query = self._query()
        subnet = query.get("subnet") or self.app.config.subnet
        started = time.time()
        candidates = discover(subnet)
        self.send_json(
            HTTPStatus.OK,
            {
                "subnet": subnet,
                "candidates": candidates,
                "duration": round(time.time() - started, 2),
            },
        )

    def _api_files(self) -> None:
        rel = self._query().get("path", "")
        listing = self.app.library.listing(rel)
        self.send_json(HTTPStatus.OK, listing)

    def _api_files_delete(self) -> None:
        data = self._read_json()
        if data is None:
            return
        raw_paths = data.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            self.send_api_error(
                HTTPStatus.BAD_REQUEST, "paths debe ser una lista no vacía"
            )
            return
        if len(raw_paths) > 500:
            self.send_api_error(
                HTTPStatus.BAD_REQUEST, "No se pueden eliminar más de 500 elementos"
            )
            return

        paths: list[str] = []
        errors: list[dict[str, str]] = []
        for raw in raw_paths:
            if not isinstance(raw, str):
                errors.append({"path": str(raw), "error": "Ruta inválida"})
                continue
            try:
                canonical = self.app.library.validate_delete(raw)
            except LibraryError as exc:
                errors.append({"path": raw, "error": str(exc)})
                continue
            if canonical not in paths:
                paths.append(canonical)
        if errors:
            self.send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": errors[0]["error"], "errors": errors},
            )
            return

        conflicts = self.app.jobs.conflicting_jobs(paths)
        if conflicts:
            names = ", ".join(
                (job.title or job.archive_path or job.package_path or job.id)[:120]
                for job in conflicts[:3]
            )
            self.send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": (
                        "Hay trabajos activos que usan esos archivos: "
                        f"{names}. Esperá a que terminen o cancelalos antes de borrar."
                    ),
                    "jobs": [
                        {
                            "id": job.id,
                            "title": job.title,
                            "state": job.state,
                        }
                        for job in conflicts
                    ],
                },
            )
            return

        deleted: list[str] = []
        errors = []
        for rel in paths:
            try:
                deleted.append(self.app.library.delete(rel))
            except LibraryError as exc:
                errors.append({"path": rel, "error": str(exc)})
        self.send_json(
            HTTPStatus.OK,
            {"deleted": deleted, "errors": errors, "count": len(deleted)},
        )

    def _api_pkg(self) -> None:
        rel = self._query().get("path", "")
        info = self.app.library.pkg_info(rel)
        data = info.to_dict()
        console_id = self._query().get("console_id", "")
        if console_id:
            console = self.app.consoles.require(console_id)
            data["firmware_ok"] = firmware_satisfied(info.firmware, console.firmware)
            data["console_firmware"] = console.firmware
        self.send_json(HTTPStatus.OK, {"pkg": data})

    def _api_icon(self) -> None:
        rel = self._query().get("path", "")
        etag = self.app.library.icon_etag(rel)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self._security_headers()
            self.end_headers()
            return
        data = self.app.library.icon(rel)
        if not data:
            self.send_bytes(
                HTTPStatus.NOT_FOUND,
                b"Sin icono",
                "text/plain; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
            return
        self.send_bytes(
            HTTPStatus.OK,
            data,
            "image/png",
            {
                "Cache-Control": "public, max-age=86400",
                "ETag": etag,
            },
        )

    def _api_jobs_list(self) -> None:
        console_id = self._query().get("console_id") or None
        jobs = self.app.jobs.list(console_id)
        self.send_json(
            HTTPStatus.OK,
            {
                "jobs": [job.to_dict() for job in jobs],
                "disk": self.app.library.disk_stats(),
            },
        )

    def _api_jobs_clear(self) -> None:
        data = self._read_json()
        if data is None:
            return
        console_id = str(data.get("console_id") or "").strip() or None
        removed = self.app.jobs.clear_terminal(console_id)
        self.send_json(HTTPStatus.OK, {"removed": removed})

    def _api_jobs_create(self) -> None:
        data = self._read_json()
        if data is None:
            return
        console_id = str(data.get("console_id") or "")
        files = data.get("files")
        if not isinstance(files, list):
            self.send_api_error(HTTPStatus.BAD_REQUEST, "files debe ser una lista")
            return
        jobs, warnings = self.app.jobs.create_install_batch(
            console_id, [str(item) for item in files], bool(data.get("include_dlc"))
        )
        self.send_json(
            HTTPStatus.CREATED,
            {"jobs": [job.to_dict() for job in jobs], "warnings": warnings},
        )

    def _api_extract(self) -> None:
        data = self._read_json()
        if data is None:
            return
        job = self.app.jobs.create_extract(
            str(data.get("path") or ""),
            delete_archive=bool(data.get("delete_archive")),
            overwrite=bool(data.get("overwrite")),
        )
        self.send_json(HTTPStatus.CREATED, {"job": job.to_dict()})

    def _api_job_action(self, job_id: str, action: str) -> None:
        method = getattr(self.app.jobs, action)
        job = method(job_id)
        self.send_json(HTTPStatus.OK, {"job": job.to_dict()})

    def _api_stats(self) -> None:
        jobs = self.app.jobs.list()
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.state] = counts.get(job.state, 0) + 1
        self.send_json(
            HTTPStatus.OK,
            {
                "disk": self.app.library.disk_stats(),
                "consoles": len(self.app.consoles.list()),
                "jobs": counts,
                "library_root": str(self.app.config.library_root),
                "public_base_url": self.app.config.public_base_url,
                "app_version": self.app.config.app_version,
            },
        )


class Ps4WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: App) -> None:
        self.app = app
        super().__init__(address, Handler)


def create_server(
    config: Config,
    *,
    host: str | None = None,
    port: int | None = None,
    start_workers: bool = True,
) -> Ps4WebServer:
    app = App(config, start_workers=start_workers)
    bind_host = config.listen_host if host is None else host
    bind_port = config.listen_port if port is None else int(port)
    return Ps4WebServer((bind_host, bind_port), app)
