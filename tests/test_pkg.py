from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from ps4web.pkg import (
    PkgError,
    extract_icon,
    firmware_satisfied,
    format_firmware,
    human_size,
    package_kind,
    parse_pkg,
    parse_sfo,
    sort_packages,
    version_tuple,
)
from tests.pkg_fixture import PNG_1X1, basic_fields, build_pkg, build_sfo


class FirmwareTests(unittest.TestCase):
    def test_format(self) -> None:
        self.assertEqual(format_firmware(0x10008000), "10.00.80")
        self.assertEqual(format_firmware(0x04700000), "4.70")
        self.assertEqual(format_firmware(0x05050000), "5.05")
        self.assertEqual(format_firmware(0), "")

    def test_version_tuple(self) -> None:
        self.assertEqual(version_tuple("10.00.80"), (10, 0, 80))
        self.assertEqual(version_tuple("5.05"), (5, 5))
        self.assertEqual(version_tuple(""), ())

    def test_satisfied(self) -> None:
        self.assertTrue(firmware_satisfied("9.00", "9.60"))
        self.assertFalse(firmware_satisfied("10.00", "9.00"))
        self.assertTrue(firmware_satisfied("5.05", "5.05"))
        self.assertIsNone(firmware_satisfied("", "9.00"))
        self.assertIsNone(firmware_satisfied("9.00", ""))

    def test_human_size(self) -> None:
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(1024), "1.0 KiB")
        self.assertEqual(human_size(1024**3 * 2), "2.0 GiB")


class KindTests(unittest.TestCase):
    def test_categories(self) -> None:
        self.assertEqual(package_kind("gd"), "base")
        self.assertEqual(package_kind("gp"), "patch")
        self.assertEqual(package_kind("ac"), "dlc")
        self.assertEqual(package_kind("gde"), "base")

    def test_flags_and_filenames(self) -> None:
        self.assertEqual(package_kind("", 0x00100000, 0x1A), "patch")
        self.assertEqual(package_kind("", 0x40000000, 0x1A), "patch")
        self.assertEqual(package_kind("", 0, 0x1B), "dlc")
        self.assertEqual(
            package_kind("", 0, 0x1A, "EP0000-CUSA00000_00-DLC000000000.pkg"),
            "dlc",
        )
        self.assertEqual(
            package_kind("", 0, 0x1A, "EP0000-CUSA00000_00-TEST-A0100-V0100.pkg"),
            "patch",
        )


class SfoTests(unittest.TestCase):
    def test_parse_round_trip(self) -> None:
        sfo = parse_sfo(build_sfo(basic_fields()))
        self.assertEqual(sfo["TITLE"], "Test Game")
        self.assertEqual(sfo["TITLE_ID"], "CUSA00000")
        self.assertEqual(sfo["SYSTEM_VER"], 0x10008000)
        self.assertEqual(sfo["CATEGORY"], "gd")

    def test_bad_magic(self) -> None:
        with self.assertRaises(PkgError):
            parse_sfo(b"nope" * 10)


class PkgTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_parse_and_icon(self) -> None:
        path = build_pkg(self.root / "game.pkg", basic_fields())
        info = parse_pkg(path)
        self.assertEqual(info.title, "Test Game")
        self.assertEqual(info.title_id, "CUSA00000")
        self.assertEqual(info.version, "01.00")
        self.assertEqual(info.category, "gd")
        self.assertEqual(info.kind, "base")
        self.assertEqual(info.firmware, "10.00.80")
        self.assertTrue(info.icon_available)
        self.assertEqual(info.size, path.stat().st_size)
        self.assertEqual(extract_icon(path), PNG_1X1)

    def test_missing_icon(self) -> None:
        path = build_pkg(self.root / "noicon.pkg", basic_fields(), include_icon_entry=False)
        info = parse_pkg(path)
        self.assertFalse(info.icon_available)
        self.assertIsNone(extract_icon(path))

    def test_patch_and_dlc(self) -> None:
        patch = build_pkg(
            self.root / "patch.pkg",
            basic_fields(CATEGORY="gp", TITLE="Test Game Patch", VERSION="01.31"),
            content_flags=0x40000000,
        )
        dlc = build_pkg(
            self.root / "dlc.pkg",
            basic_fields(CATEGORY="ac", TITLE="Test DLC", TITLE_ID="CUSA00001"),
            content_type=0x1B,
        )
        self.assertEqual(parse_pkg(patch).kind, "patch")
        self.assertEqual(parse_pkg(dlc).kind, "dlc")

    def test_bad_magic(self) -> None:
        path = self.root / "bad.pkg"
        path.write_bytes(b"not a pkg" * 1000)
        with self.assertRaises(PkgError):
            parse_pkg(path)

    def test_truncated(self) -> None:
        path = self.root / "trunc.pkg"
        path.write_bytes(b"\x7fCNT" + b"\x00" * 40)
        with self.assertRaises(PkgError):
            parse_pkg(path)

    def test_table_out_of_bounds(self) -> None:
        path = self.root / "badtable.pkg"
        data = bytearray(0x2000)
        data[:4] = b"\x7fCNT"
        struct.pack_into(">I", data, 0x10, 1000)
        struct.pack_into(">I", data, 0x18, 0x1F00)
        path.write_bytes(bytes(data))
        with self.assertRaises(PkgError):
            parse_pkg(path)

    def test_sort(self) -> None:
        base = build_pkg(self.root / "b.pkg", basic_fields())
        patch = build_pkg(
            self.root / "p.pkg",
            basic_fields(CATEGORY="gp", VERSION="01.31"),
            content_flags=0x40000000,
        )
        dlc = build_pkg(
            self.root / "d.pkg",
            basic_fields(CATEGORY="ac", TITLE="DLC", VERSION="01.00"),
            content_type=0x1B,
        )
        infos = [parse_pkg(dlc), parse_pkg(patch), parse_pkg(base)]
        sorted_infos = sort_packages(infos)
        self.assertEqual([item.kind for item in sorted_infos], ["base", "patch", "dlc"])

    @unittest.skipUnless(Path("/tmp/opencode/rpi.pkg").is_file(), "RPI PKG no disponible")
    def test_real_rpi_pkg(self) -> None:
        info = parse_pkg("/tmp/opencode/rpi.pkg")
        self.assertEqual(info.title_id, "FLTZ00003")
        self.assertEqual(info.content_id, "UP9000-FLTZ00003_00-REMOTEPKGINSTALL")
        self.assertTrue(info.icon_available)
        icon = extract_icon("/tmp/opencode/rpi.pkg")
        self.assertIsNotNone(icon)
        assert icon is not None
        self.assertTrue(icon.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
