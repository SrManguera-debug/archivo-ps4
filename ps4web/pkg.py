"""Minimal, safe parser for PS4 PKG files and their embedded SFO/icon."""

from __future__ import annotations

import hashlib
import os
import re
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

PKG_MAGIC = b"\x7fCNT"
SFO_MAGIC = b"\x00PSF"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ENTRY_PARAM_SFO = 0x1000
ENTRY_ICON0_PNG = 0x1200
ENTRY_PIC0_PNG = 0x1220
MAX_SFO_SIZE = 1024 * 1024
MAX_ICON_SIZE = 16 * 1024 * 1024
HEADER_SIZE = 0x2000


class PkgError(RuntimeError):
    """Raised when a PKG cannot be parsed."""


@dataclass(frozen=True)
class PkgEntry:
    id: int
    offset: int
    size: int


@dataclass
class PkgInfo:
    path: str
    title: str = ""
    title_id: str = ""
    content_id: str = ""
    version: str = ""
    app_ver: str = ""
    category: str = ""
    system_ver: int = 0
    firmware: str = ""
    content_type: int = 0
    content_flags: int = 0
    size: int = 0
    mtime: float = 0.0
    kind: str = "other"
    icon_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["filename"] = os.path.basename(self.path)
        data["firmware_tuple"] = list(version_tuple(self.firmware))
        return data


@dataclass
class PkgHeader:
    magic: bytes
    entry_count: int
    sc_entry_count: int
    entry_table_offset: int
    body_offset: int
    body_size: int
    content_id: str
    content_type: int
    content_flags: int
    package_size: int
    file_size: int


def _read_exact(fh: BinaryIO, size: int) -> bytes:
    data = fh.read(size)
    if len(data) != size:
        raise PkgError("PKG truncado: no se pudieron leer los bytes esperados")
    return data


def read_header(fh: BinaryIO) -> PkgHeader:
    fh.seek(0)
    head = fh.read(HEADER_SIZE)
    if len(head) < 0x80:
        raise PkgError("Archivo demasiado pequeño para ser un PKG")
    if head[:4] != PKG_MAGIC:
        raise PkgError("Firma PKG inválida (se esperaba \\x7fCNT)")
    entry_count = struct.unpack_from(">I", head, 0x10)[0]
    sc_entry_count = struct.unpack_from(">H", head, 0x14)[0]
    entry_table_offset = struct.unpack_from(">I", head, 0x18)[0]
    body_offset = struct.unpack_from(">Q", head, 0x20)[0]
    body_size = struct.unpack_from(">Q", head, 0x28)[0]
    content_id = head[0x40:0x64].split(b"\x00", 1)[0].decode("ascii", "replace")
    content_type = struct.unpack_from(">I", head, 0x74)[0]
    content_flags = struct.unpack_from(">I", head, 0x78)[0]
    package_size = struct.unpack_from(">Q", head, 0x430)[0]
    fh.seek(0, os.SEEK_END)
    file_size = fh.tell()
    if entry_count > 100_000:
        raise PkgError(f"Cantidad de entradas absurda: {entry_count}")
    if entry_table_offset <= 0 or entry_table_offset >= file_size:
        raise PkgError("Offset de tabla de entradas fuera del archivo")
    if entry_count * 0x20 > file_size - entry_table_offset:
        raise PkgError("Tabla de entradas truncada")
    return PkgHeader(
        magic=head[:4],
        entry_count=entry_count,
        sc_entry_count=sc_entry_count,
        entry_table_offset=entry_table_offset,
        body_offset=body_offset,
        body_size=body_size,
        content_id=content_id,
        content_type=content_type,
        content_flags=content_flags,
        package_size=package_size,
        file_size=file_size,
    )


def read_entries(fh: BinaryIO, header: PkgHeader) -> dict[int, PkgEntry]:
    fh.seek(header.entry_table_offset)
    table = _read_exact(fh, header.entry_count * 0x20)
    entries: dict[int, PkgEntry] = {}
    for index in range(header.entry_count):
        base = index * 0x20
        entry_id = struct.unpack_from(">I", table, base)[0]
        offset = struct.unpack_from(">I", table, base + 0x10)[0]
        size = struct.unpack_from(">I", table, base + 0x14)[0]
        if offset > header.file_size or size > header.file_size - offset:
            # Keep parsing: unrelated entries may be encrypted or padded.
            continue
        entries[entry_id] = PkgEntry(id=entry_id, offset=offset, size=size)
    return entries


def _entry_bytes(fh: BinaryIO, header: PkgHeader, entry: PkgEntry, magic: bytes) -> bytes:
    candidates = [entry.offset]
    relative = header.body_offset + entry.offset
    if relative != entry.offset:
        candidates.append(relative)
    for offset in candidates:
        if offset < 0 or offset + len(magic) > header.file_size:
            continue
        fh.seek(offset)
        head = fh.read(len(magic))
        if head != magic:
            continue
        remaining = min(entry.size, header.file_size - offset)
        fh.seek(offset)
        return fh.read(remaining)
    raise PkgError("Entrada PKG no encontrada o con firma inválida")


def _decode_sfo_string(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()


def parse_sfo(raw: bytes) -> dict[str, Any]:
    if len(raw) < 20 or raw[:4] != SFO_MAGIC:
        raise PkgError("param.sfo inválido")
    version, key_table, data_table, count = struct.unpack_from("<IIII", raw, 4)
    del version
    if count > 4096:
        raise PkgError("param.sfo con demasiadas claves")
    if key_table >= len(raw) or data_table > len(raw) or data_table < 20:
        raise PkgError("param.sfo con offsets inválidos")
    if 20 + count * 16 > len(raw):
        raise PkgError("param.sfo con tabla truncada")
    result: dict[str, Any] = {}
    for index in range(count):
        base = 20 + index * 16
        key_offset, fmt, data_len, data_max, data_offset = struct.unpack_from(
            "<HHIII", raw, base
        )
        if data_len > data_max:
            continue
        key_start = key_table + key_offset
        if key_start >= len(raw):
            continue
        key_end = raw.find(b"\x00", key_start, min(len(raw), key_start + 256))
        if key_end < 0:
            continue
        key = raw[key_start:key_end].decode("ascii", "replace")
        start = data_table + data_offset
        if start + data_len > len(raw):
            continue
        value = raw[start : start + data_len]
        if fmt == 0x0404:
            result[key] = _decode_sfo_string(value)
        elif fmt == 0x0402:
            if len(value) >= 4:
                result[key] = struct.unpack_from("<I", value, 0)[0]
            elif len(value) >= 2:
                result[key] = struct.unpack_from("<H", value, 0)[0]
        elif fmt == 0x0004:
            result[key] = value
        # Other formats are left out intentionally.
    return result


def _bcd_byte(value: int) -> int | None:
    high, low = value >> 4, value & 0x0F
    if high > 9 or low > 9:
        return None
    return high * 10 + low


def format_firmware(system_ver: int) -> str:
    if not system_ver:
        return ""
    raw = struct.pack(">I", system_ver & 0xFFFFFFFF)
    parts = [_bcd_byte(b) for b in raw]
    if any(part is None for part in parts):
        # Some homebrew packages use plain integers; show something sane.
        parts = list(raw)
    assert all(part is not None for part in parts)
    major = parts[0]
    minor = parts[1] or 0
    build = parts[2] or 0
    text = f"{major}.{minor:02d}"
    if build:
        text += f".{build}"
    return text


def version_tuple(text: str) -> tuple[int, ...]:
    if not text:
        return ()
    matches = re.findall(r"\d+", str(text))
    return tuple(int(part) for part in matches[:4])


def _content_id_version(content_id: str) -> str:
    if not content_id or "-" not in content_id:
        return ""
    label = content_id.rsplit("-", 1)[-1]
    match = re.search(r"V(\d{4})", label)
    return match.group(1) if match else ""


def _title_id_from_content_id(content_id: str) -> str:
    if not content_id or "-" not in content_id:
        return ""
    try:
        service = content_id.split("-", 1)[1]
        return service.split("_", 1)[0]
    except IndexError:
        return ""


def package_kind(
    category: str = "",
    content_flags: int = 0,
    content_type: int = 0,
    filename: str = "",
) -> str:
    cat = (category or "").lower()
    if cat.startswith("gp"):
        return "patch"
    if cat.startswith("gd") or cat.startswith("gde"):
        return "base"
    if cat.startswith("ac") or cat.startswith("al"):
        return "dlc"
    flags = content_flags or 0
    if flags & 0x40000000 or flags & 0x41000000 == 0x41000000 or flags & 0x60000000 == 0x60000000:
        return "patch"
    if flags & 0x00100000:
        return "patch"
    if content_type == 0x1E:
        return "patch"
    if content_type in (0x1B, 0x1C):
        return "dlc"
    name = os.path.basename(filename or "").upper()
    if "-AC" in name or "DLC" in name or "_DLC" in name:
        return "dlc"
    if re.search(r"-A(?!C)\d{4}-", name) or re.search(r"-A\d{4}-V", name):
        return "patch"
    if content_type == 0x1A:
        return "base"
    return "other"


def has_icon(fh: BinaryIO, header: PkgHeader, entries: dict[int, PkgEntry]) -> bool:
    entry = entries.get(ENTRY_ICON0_PNG) or entries.get(ENTRY_PIC0_PNG)
    if entry is None or entry.size > MAX_ICON_SIZE:
        return False
    try:
        _entry_bytes(fh, header, entry, PNG_MAGIC)
        return True
    except PkgError:
        return False


def parse_pkg(path: str | os.PathLike[str]) -> PkgInfo:
    file_path = Path(path)
    try:
        stat = file_path.stat()
    except OSError as exc:
        raise PkgError(f"No se puede leer {file_path}: {exc}") from exc
    if not file_path.is_file():
        raise PkgError(f"No es un archivo: {file_path}")
    try:
        with file_path.open("rb") as fh:
            header = read_header(fh)
            entries = read_entries(fh, header)
            sfo_entry = entries.get(ENTRY_PARAM_SFO)
            if sfo_entry is None:
                raise PkgError("El PKG no contiene param.sfo")
            if sfo_entry.size > MAX_SFO_SIZE:
                raise PkgError("param.sfo sospechosamente grande")
            raw_sfo = _entry_bytes(fh, header, sfo_entry, SFO_MAGIC)
            sfo = parse_sfo(raw_sfo)
            icon = has_icon(fh, header, entries)
    except OSError as exc:
        raise PkgError(f"No se puede leer {file_path}: {exc}") from exc
    title = str(sfo.get("TITLE", "") or sfo.get("TITLE_ES", "") or "")
    title_id = str(sfo.get("TITLE_ID", "") or _title_id_from_content_id(header.content_id))
    content_id = str(sfo.get("CONTENT_ID", "") or header.content_id)
    version = str(sfo.get("VERSION", "") or "")
    app_ver = str(sfo.get("APP_VER", "") or "")
    category = str(sfo.get("CATEGORY", "") or "")
    system_ver = int(sfo.get("SYSTEM_VER", 0) or 0)
    kind = package_kind(category, header.content_flags, header.content_type, file_path.name)
    info = PkgInfo(
        path=str(file_path),
        title=title or file_path.stem,
        title_id=title_id,
        content_id=content_id,
        version=version or _content_id_version(content_id),
        app_ver=app_ver,
        category=category,
        system_ver=system_ver,
        firmware=format_firmware(system_ver),
        content_type=header.content_type,
        content_flags=header.content_flags,
        size=stat.st_size,
        mtime=stat.st_mtime,
        kind=kind,
        icon_available=icon,
    )
    return info


def extract_icon(path: str | os.PathLike[str]) -> bytes | None:
    file_path = Path(path)
    try:
        with file_path.open("rb") as fh:
            header = read_header(fh)
            entries = read_entries(fh, header)
            for entry_id in (ENTRY_ICON0_PNG, ENTRY_PIC0_PNG):
                entry = entries.get(entry_id)
                if entry is None or entry.size <= 0 or entry.size > MAX_ICON_SIZE:
                    continue
                try:
                    data = _entry_bytes(fh, header, entry, PNG_MAGIC)
                except PkgError:
                    continue
                if data.startswith(PNG_MAGIC):
                    return data
    except (OSError, PkgError):
        return None
    return None


def icon_cache_key(path: str | os.PathLike[str]) -> str:
    file_path = Path(path)
    try:
        stat = file_path.stat()
    except OSError:
        return ""
    value = f"{file_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    return hashlib.sha256(value).hexdigest()


def sort_packages(infos: list[PkgInfo]) -> list[PkgInfo]:
    rank = {"base": 0, "patch": 1, "dlc": 2, "other": 3}

    def key(info: PkgInfo) -> tuple[Any, ...]:
        group = info.title_id or info.title.lower()
        return (
            group,
            rank.get(info.kind, 3),
            version_tuple(info.version or info.app_ver),
            os.path.basename(info.path).lower(),
        )

    return sorted(infos, key=key)


def firmware_satisfied(required: str, console_firmware: str) -> bool | None:
    """True/False when both values are comparable, None when unknown."""
    if not required or not console_firmware:
        return None
    req = version_tuple(required)
    cur = version_tuple(console_firmware)
    if not req or not cur:
        return None
    length = max(len(req), len(cur))
    req = req + (0,) * (length - len(req))
    cur = cur + (0,) * (length - len(cur))
    return cur >= req


def human_size(size: int | float) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"
