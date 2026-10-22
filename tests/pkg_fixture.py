"""Synthetic PS4 PKG builder used by tests."""

from __future__ import annotations

import struct
from pathlib import Path

# 1x1 transparent PNG.
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def build_sfo(fields: dict[str, str | int]) -> bytes:
    entries: list[tuple[str, int, bytes]] = []
    for key, value in fields.items():
        if isinstance(value, int):
            raw = struct.pack("<I", value)
            fmt = 0x0402
        else:
            raw = value.encode("utf-8") + b"\x00"
            fmt = 0x0404
        entries.append((key, fmt, raw))
    count = len(entries)
    key_table_offset = 20 + count * 16
    key_table = b""
    key_offsets = []
    for key, _, _ in entries:
        key_offsets.append(len(key_table))
        key_table += key.encode("ascii") + b"\x00"
    data_table_offset = key_table_offset + len(key_table)
    data_table = b""
    data_offsets = []
    for _, _, raw in entries:
        data_offsets.append(len(data_table))
        data_table += raw
        if len(data_table) % 4:
            data_table += b"\x00" * (4 - len(data_table) % 4)
    header = b"\x00PSF" + struct.pack(
        "<IIII", 0x101, key_table_offset, data_table_offset, count
    )
    table = b""
    for index, (_, fmt, raw) in enumerate(entries):
        padded = len(raw) + (-len(raw) % 4)
        table += struct.pack(
            "<HHIII",
            key_offsets[index],
            fmt,
            len(raw),
            padded,
            data_offsets[index],
        )
    return header + table + key_table + data_table


def build_pkg(
    path: Path,
    fields: dict[str, str | int],
    icon: bytes | None = PNG_1X1,
    include_icon_entry: bool = True,
    content_flags: int = 0x0A000000,
    content_type: int = 0x1A,
) -> Path:
    sfo = build_sfo(fields)
    header_size = 0x2000
    table_offset = 0x400
    sfo_offset = 0x600
    icon_offset = 0x1000
    entries = [(0x1000, sfo_offset, len(sfo))]
    if include_icon_entry and icon is not None:
        entries.append((0x1200, icon_offset, len(icon)))
    header = bytearray(header_size)
    header[0:4] = b"\x7fCNT"
    struct.pack_into(">I", header, 0x10, len(entries))
    struct.pack_into(">H", header, 0x14, 0)
    struct.pack_into(">I", header, 0x18, table_offset)
    struct.pack_into(">Q", header, 0x20, 0x2000)
    struct.pack_into(">Q", header, 0x28, 0)
    content_id = str(fields.get("CONTENT_ID", "UP0000-CUSA00000_00-TEST0000000000"))
    header[0x40 : 0x40 + len(content_id)] = content_id.encode("ascii")[:0x24]
    struct.pack_into(">I", header, 0x74, content_type)
    struct.pack_into(">I", header, 0x78, content_flags)
    for index, (entry_id, offset, size) in enumerate(entries):
        base = table_offset + index * 0x20
        struct.pack_into(">I", header, base, entry_id)
        struct.pack_into(">I", header, base + 0x10, offset)
        struct.pack_into(">I", header, base + 0x14, size)
    body = bytearray(header_size)
    body[0:header_size] = header
    body[sfo_offset : sfo_offset + len(sfo)] = sfo
    if include_icon_entry and icon is not None:
        body[icon_offset : icon_offset + len(icon)] = icon
    path.write_bytes(bytes(body))
    return path


def basic_fields(**overrides: str | int) -> dict[str, str | int]:
    fields: dict[str, str | int] = {
        "TITLE": "Test Game",
        "TITLE_ID": "CUSA00000",
        "CONTENT_ID": "UP0000-CUSA00000_00-TEST0000000000",
        "VERSION": "01.00",
        "APP_VER": "01.00",
        "CATEGORY": "gd",
        "SYSTEM_VER": 0x10008000,
    }
    fields.update(overrides)
    return fields
