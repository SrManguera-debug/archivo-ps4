from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ps4web.library import Library, LibraryError
from tests.pkg_fixture import PNG_1X1, basic_fields, build_pkg


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "library"
        self.root.mkdir()
        self.cache = Path(self.tmp.name) / "cache"
        self.library = Library(self.root, self.cache)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _game(self, name: str = "game.pkg", **fields: str | int) -> Path:
        return build_pkg(self.root / name, basic_fields(**fields))

    def test_resolve_root_and_nested(self) -> None:
        nested = self.root / "Game"
        nested.mkdir()
        self.assertEqual(self.library.resolve(""), self.root)
        self.assertEqual(self.library.resolve("Game"), nested)
        self.assertEqual(self.library.relpath(nested), "Game")

    def test_rejects_traversal_and_absolute(self) -> None:
        for value in ("..", "../etc/passwd", "/etc/passwd", "Game/../../etc", "a\x00b"):
            with self.assertRaises(LibraryError, msg=value):
                self.library.resolve(value)

    def test_rejects_symlink_escape(self) -> None:
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        link = self.root / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        with self.assertRaises(LibraryError):
            self.library.resolve("escape")

    def test_delete_file_and_folder(self) -> None:
        archive = self.root / "old.rar"
        archive.write_bytes(b"old")
        folder = self.root / "Old Game"
        folder.mkdir()
        (folder / "leftover.pkg").write_bytes(b"pkg")
        self.assertEqual(self.library.delete("old.rar"), "old.rar")
        self.assertEqual(self.library.delete("Old Game"), "Old Game")
        self.assertFalse(archive.exists())
        self.assertFalse(folder.exists())
        with self.assertRaises(LibraryError):
            self.library.delete("old.rar")

    def test_delete_rejects_root_traversal_and_symlink(self) -> None:
        for value in ("", ".", "..", "../outside"):
            with self.assertRaises(LibraryError, msg=value):
                self.library.delete(value)
        target = self.root / "target.txt"
        target.write_text("keep", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        with self.assertRaises(LibraryError):
            self.library.delete("link.txt")
        self.assertTrue(target.exists())

    def test_listing_and_metadata(self) -> None:
        game = self._game()
        dlc_dir = self.root / "DLC"
        dlc_dir.mkdir()
        build_pkg(dlc_dir / "extra.pkg", basic_fields(CATEGORY="ac", TITLE_ID="CUSA00001"))
        (self.root / "archive.rar").write_bytes(b"Rar!")
        (self.root / ".hidden").write_text("x")
        (self.root / "notes.txt").write_text("hello")
        listing = self.library.listing("")
        names = [item["name"] for item in listing["entries"]]
        self.assertEqual(names, ["DLC", "archive.rar", "notes.txt", "game.pkg"])
        self.assertEqual(listing["breadcrumb"], [{"name": "Biblioteca", "path": ""}])
        pkg_entry = next(item for item in listing["entries"] if item["name"] == "game.pkg")
        self.assertEqual(pkg_entry["type"], "pkg")
        self.assertEqual(pkg_entry["title"], "Test Game")
        self.assertEqual(pkg_entry["firmware"], "10.00.80")
        self.assertIn("/api/icon?path=game.pkg", pkg_entry["icon_url"])
        self.assertEqual(listing["entries"][0]["pkg_count"], 1)
        self.assertEqual(listing["entries"][1]["type"], "rar")
        self.assertIn("disk", listing)
        self.assertEqual(self.library.pkg_info("game.pkg").size, game.stat().st_size)

    def test_nested_listing_breadcrumb(self) -> None:
        nested = self.root / "A" / "B"
        nested.mkdir(parents=True)
        self._game("A/B/game.pkg")
        listing = self.library.listing("A/B")
        self.assertEqual(listing["path"], "A/B")
        self.assertEqual(listing["parent"], "A")
        self.assertEqual(
            listing["breadcrumb"],
            [
                {"name": "Biblioteca", "path": ""},
                {"name": "A", "path": "A"},
                {"name": "B", "path": "A/B"},
            ],
        )
        self.assertEqual(listing["entries"][0]["path"], "A/B/game.pkg")

    def test_invalid_pkg_does_not_break_listing(self) -> None:
        (self.root / "broken.pkg").write_bytes(b"nope" * 1000)
        listing = self.library.listing("")
        entry = listing["entries"][0]
        self.assertEqual(entry["type"], "pkg")
        self.assertIn("metadata_error", entry)
        with self.assertRaises(LibraryError):
            self.library.pkg_info("broken.pkg")

    def test_icon_cache(self) -> None:
        self._game()
        data = self.library.icon("game.pkg")
        self.assertEqual(data, PNG_1X1)
        files = list(self.library.icons_dir.glob("*.png"))
        self.assertEqual(len(files), 1)
        self.assertEqual(self.library.icon("game.pkg"), PNG_1X1)
        self.assertEqual(len(list(self.library.icons_dir.glob("*.png"))), 1)

    def test_scan_packages_sorted(self) -> None:
        base = self._game("base.pkg", VERSION="01.00")
        patch = self._game("patch.pkg", VERSION="01.31", CATEGORY="gp")
        dlc = self._game("dlc.pkg", VERSION="01.00", CATEGORY="ac", TITLE_ID="CUSA00001")
        infos, errors = self.library.scan_packages("")
        self.assertEqual(errors, [])
        self.assertEqual([item.path for item in infos], [str(base), str(patch), str(dlc)])
        self.assertEqual([item.kind for item in infos], ["base", "patch", "dlc"])

    def test_scan_single_pkg(self) -> None:
        game = self._game("one.pkg")
        infos, errors = self.library.scan_packages("one.pkg")
        self.assertEqual(errors, [])
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].path, str(game))

    def test_pkg_url_encodes(self) -> None:
        folder = self.root / "Street Fighter 6 [v1]"
        folder.mkdir()
        game = build_pkg(folder / "game [x].pkg", basic_fields())
        url = self.library.pkg_url("Street Fighter 6 [v1]/game [x].pkg", "http://192.168.1.20/")
        self.assertIn("Street%20Fighter%206%20%5Bv1%5D", url)
        self.assertTrue(url.startswith("http://192.168.1.20/"))
        self.assertTrue(game.is_file())

    def test_archive_info(self) -> None:
        archive = self.root / "x.rar"
        archive.write_bytes(b"x" * 2048)
        info = self.library.archive_info("x.rar")
        self.assertEqual(info["extension"], ".rar")
        self.assertEqual(info["size"], 2048)

    def test_invalidate(self) -> None:
        self._game()
        self.library.pkg_info("game.pkg")
        self.assertGreater(len(self.library._pkg_cache), 0)
        self.library.invalidate("")
        self.assertEqual(len(self.library._pkg_cache), 0)

    def test_disk_stats(self) -> None:
        stats = self.library.disk_stats()
        self.assertGreater(stats["total"], 0)
        self.assertIn("used_human", stats)


if __name__ == "__main__":
    unittest.main()
