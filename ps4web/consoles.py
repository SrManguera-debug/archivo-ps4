"""Console registry, LAN discovery, port probing and RPI installer upload."""

from __future__ import annotations

import ftplib
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

LOGGER = logging.getLogger(__name__)
RPI_PORTS = (12800, 12801)
REMOTE_PLAY_PORT = 9295
DEFAULT_FTP_PORT = 2121


class ConsoleError(RuntimeError):
    """Invalid console data or a failed console operation."""


@dataclass
class Console:
    id: str
    name: str
    ip: str
    rpi_port: int = 12800
    ftp_port: int = DEFAULT_FTP_PORT
    firmware: str = ""
    notes: str = ""
    created_at: float = field(default_factory=time.time)

    @classmethod
    def create(
        cls,
        name: str,
        ip: str,
        rpi_port: int = 12800,
        ftp_port: int = DEFAULT_FTP_PORT,
        firmware: str = "",
        notes: str = "",
    ) -> "Console":
        return cls(
            id=uuid.uuid4().hex,
            name=name,
            ip=ip,
            rpi_port=rpi_port,
            ftp_port=ftp_port,
            firmware=firmware,
            notes=notes,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Console":
        if not isinstance(data, dict):
            raise ConsoleError("Datos de consola inválidos")
        try:
            console = cls(
                id=str(data.get("id") or uuid.uuid4().hex),
                name=str(data.get("name", "")),
                ip=str(data.get("ip", "")),
                rpi_port=int(data.get("rpi_port", 12800)),
                ftp_port=int(data.get("ftp_port", DEFAULT_FTP_PORT)),
                firmware=str(data.get("firmware", "") or ""),
                notes=str(data.get("notes", "") or ""),
                created_at=float(data.get("created_at") or time.time()),
            )
        except (TypeError, ValueError) as exc:
            raise ConsoleError(f"Datos de consola inválidos: {exc}") from exc
        validate_console(console)
        return console


def validate_ip(value: str) -> str:
    text = (value or "").strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError as exc:
        raise ConsoleError(f"IP inválida: {value!r}") from exc
    if address.version != 4:
        raise ConsoleError("Solo se admiten direcciones IPv4")
    if address.is_unspecified or address.is_multicast:
        raise ConsoleError(f"IP no utilizable: {text}")
    return text


def validate_port(value: Any, label: str = "puerto") -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ConsoleError(f"{label} inválido: {value!r}") from exc
    if not (1 <= port <= 65535):
        raise ConsoleError(f"{label} fuera de rango: {port}")
    return port


def validate_console(console: Console) -> None:
    console.name = (console.name or "").strip()
    if not (1 <= len(console.name) <= 64):
        raise ConsoleError("El nombre debe tener entre 1 y 64 caracteres")
    console.ip = validate_ip(console.ip)
    console.rpi_port = validate_port(console.rpi_port, "puerto RPI")
    console.ftp_port = validate_port(console.ftp_port, "puerto FTP")
    console.firmware = (console.firmware or "").strip()
    if console.firmware and not re.fullmatch(r"\d{1,2}(\.\d{1,2}){0,2}", console.firmware):
        raise ConsoleError("Firmware inválido (ejemplos: 9.00, 10.00, 5.05)")
    console.notes = (console.notes or "").strip()[:500]


class ConsoleStore:
    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.path = Path(state_dir) / "consoles.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._consoles: dict[str, Console] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                items = raw.get("consoles", [])
            else:
                items = raw
            if not isinstance(items, list):
                raise ValueError("root must be a list")
            loaded: dict[str, Console] = {}
            for item in items:
                console = Console.from_dict(item)
                loaded[console.id] = console
            self._consoles = loaded
        except Exception as exc:
            backup = self.path.with_suffix(f".corrupt-{int(time.time())}")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            LOGGER.error("consoles.json inválido (%s); respaldado en %s", exc, backup)
            self._consoles = {}

    def _save(self) -> None:
        payload = json.dumps(
            {"consoles": [console.to_dict() for console in self._consoles.values()]},
            indent=2,
            sort_keys=True,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".consoles-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def list(self) -> list[Console]:
        with self._lock:
            return sorted(self._consoles.values(), key=lambda c: (c.name.casefold(), c.ip))

    def get(self, console_id: str) -> Console | None:
        with self._lock:
            return self._consoles.get(console_id)

    def require(self, console_id: str) -> Console:
        console = self.get(console_id)
        if console is None:
            raise ConsoleError(f"Consola no encontrada: {console_id}")
        return console

    def add(
        self,
        name: str,
        ip: str,
        rpi_port: int = 12800,
        ftp_port: int = DEFAULT_FTP_PORT,
        firmware: str = "",
        notes: str = "",
    ) -> Console:
        console = Console.create(name, ip, rpi_port, ftp_port, firmware, notes)
        validate_console(console)
        with self._lock:
            for existing in self._consoles.values():
                if existing.ip == console.ip and existing.rpi_port == console.rpi_port:
                    raise ConsoleError(
                        f"Ya existe una consola en {console.ip}:{console.rpi_port}"
                    )
            self._consoles[console.id] = console
            self._save()
        return console

    def update(self, console_id: str, **fields: Any) -> Console:
        with self._lock:
            current = self._consoles.get(console_id)
            if current is None:
                raise ConsoleError(f"Consola no encontrada: {console_id}")
            data = current.to_dict()
            for key in ("name", "ip", "rpi_port", "ftp_port", "firmware", "notes"):
                if key in fields:
                    data[key] = fields[key]
            updated = Console.from_dict(data)
            for existing in self._consoles.values():
                if existing.id != console_id and existing.ip == updated.ip and existing.rpi_port == updated.rpi_port:
                    raise ConsoleError(
                        f"Ya existe una consola en {updated.ip}:{updated.rpi_port}"
                    )
            self._consoles[console_id] = updated
            self._save()
        return updated

    def delete(self, console_id: str) -> None:
        with self._lock:
            if console_id not in self._consoles:
                raise ConsoleError(f"Consola no encontrada: {console_id}")
            del self._consoles[console_id]
            self._save()


def _port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_ports(ip: str, ports: Iterable[int], timeout: float = 0.4) -> dict[int, bool]:
    unique = list(dict.fromkeys(int(port) for port in ports))
    result: dict[int, bool] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(unique)))) as pool:
        futures = {pool.submit(_port_open, ip, port, timeout): port for port in unique}
        for future in as_completed(futures):
            port = futures[future]
            try:
                result[port] = bool(future.result())
            except Exception:
                result[port] = False
    return result


def probe(console: Console, timeout: float = 0.4) -> dict[str, Any]:
    ports = set(RPI_PORTS) | {console.rpi_port, console.ftp_port, REMOTE_PLAY_PORT}
    opened = check_ports(console.ip, sorted(ports), timeout=timeout)
    rpi_port = None
    for port in (console.rpi_port, 12800, 12801):
        if opened.get(port):
            rpi_port = port
            break
    rpi = rpi_port is not None
    ftp = bool(opened.get(console.ftp_port))
    remote_play = bool(opened.get(REMOTE_PLAY_PORT))
    return {
        "ip": console.ip,
        "rpi": rpi,
        "rpi_port": rpi_port,
        "ftp": ftp,
        "ftp_port": console.ftp_port,
        "remote_play": remote_play,
        "online": rpi or ftp or remote_play,
        "checked_at": time.time(),
        "open_ports": sorted(port for port, is_open in opened.items() if is_open),
    }


def local_ips() -> set[str]:
    ips: set[str] = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.168.1.1", 9))
            ips.add(sock.getsockname()[0])
    except OSError:
        pass
    return ips


def discover(
    subnet: str,
    ports: Iterable[int] = (12800, 12801, 2121, 9295),
    timeout: float = 0.25,
    max_workers: int = 64,
    exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError as exc:
        raise ConsoleError(f"Subred inválida: {subnet!r}") from exc
    if network.version != 4:
        raise ConsoleError("La búsqueda automática solo soporta IPv4")
    if network.num_addresses > 1024:
        raise ConsoleError("La subred es demasiado grande (máximo 1024 direcciones)")
    ports = [validate_port(port) for port in ports]
    excluded = set(exclude or set()) | local_ips()
    hosts = [str(host) for host in network.hosts() if str(host) not in excluded]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(check_ports, host, ports, timeout): host for host in hosts
        }
        for future in as_completed(futures):
            host = futures[future]
            try:
                opened = future.result()
            except Exception:
                continue
            open_ports = sorted(port for port, is_open in opened.items() if is_open)
            if open_ports:
                results.append(
                    {
                        "ip": host,
                        "ports": open_ports,
                        "likely_ps4": any(port in open_ports for port in (12800, 12801, 9295)),
                    }
                )
    results.sort(key=lambda item: tuple(int(part) for part in item["ip"].split(".")))
    return results


def prepare_rpi(console: Console, rpi_pkg: str | os.PathLike[str], timeout: float = 10.0) -> str:
    path = Path(rpi_pkg)
    if not path.is_file():
        raise ConsoleError(f"No existe el instalador RPI: {path}")
    if path.suffix.lower() != ".pkg":
        raise ConsoleError("El instalador RPI debe ser un archivo .pkg")
    remote_name = path.name
    ftp = ftplib.FTP()
    try:
        ftp.connect(console.ip, console.ftp_port, timeout=timeout)
        ftp.login("anonymous", "anonymous")
        ftp.set_pasv(True)
        remote_dir = "/data/pkg"
        try:
            ftp.cwd(remote_dir)
        except ftplib.all_errors:
            try:
                ftp.cwd("/data")
                ftp.mkd("pkg")
                ftp.cwd("pkg")
            except ftplib.all_errors as exc:
                raise ConsoleError(
                    "No se pudo entrar a /data/pkg por FTP. "
                    "¿Está GoldHen con el servidor FTP activo?"
                ) from exc
        with path.open("rb") as fh:
            ftp.storbinary(f"STOR {remote_name}", fh, blocksize=64 * 1024)
        return f"{remote_dir}/{remote_name}"
    except ConsoleError:
        raise
    except ftplib.all_errors as exc:
        raise ConsoleError(f"Fallo la subida por FTP a {console.ip}:{console.ftp_port}: {exc}") from exc
    finally:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


def console_public(console: Console, status: dict[str, Any] | None = None) -> dict[str, Any]:
    data = console.to_dict()
    if status is not None:
        data["status"] = status
    return data
