"""Safe browsing of the PKG library and metadata/icon caches."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from .pkg import (
    PkgError,
    PkgInfo,
    extract_icon,
    human_size,
    icon_cache_key,
    parse_pkg,
    sort_packages,
)

ARCHIVE_EXTENSIONS = {".rar", ".7z", ".zip"}
PKG_EXTENSIONS = {".pkg"}


class LibraryError(RuntimeError):
    """Invalid path or library operation."""


class Library:
    def __init__(self, root: str | os.PathLike[str], cache_dir: str | os.PathLike[str]) -> None:
        self.root = Path(os.path.realpath(str(root)))
        self.cache_dir = Path(cache_dir)
        self.icons_dir = self.cache_dir / "icons"
        self._lock = threading.RLock()
        self._pkg_cache: OrderedDict[str, tuple[int, int, PkgInfo]] = OrderedDict()
        self._cache_limit = 5000
        self.icons_dir.mkdir(parents=True, exist_ok=True)

    # ---------- paths ----------

    def resolve(self, rel: str | None) -> Path:
        rel = (rel or "").strip()
        if "\x00" in rel:
            raise LibraryError("Ruta inválida")
        if rel.startswith(("/", "\\")):
            raise LibraryError("La ruta debe ser relativa a la biblioteca")
        parts = Path(rel).parts
        if any(part == ".." for part in parts):
            raise LibraryError("La ruta no puede contener '..'")
        candidate = os.path.realpath(os.path.join(self.root, rel))
        try:
            inside = os.path.commonpath([str(self.root), candidate]) == str(self.root)
        except ValueError:
            inside = False
        if not inside:
            raise LibraryError("La ruta sale de la biblioteca")
        return Path(candidate)

    def _resolve_any(self, value: str | os.PathLike[str]) -> Path:
        text = str(value)
        if os.path.isabs(text):
            real = Path(os.path.realpath(text))
            try:
                inside = os.path.commonpath([str(self.root), str(real)]) == str(self.root)
            except ValueError:
                inside = False
            if not inside:
                raise LibraryError("La ruta sale de la biblioteca")
            return real
        return self.resolve(text)

    def relpath(self, path: str | os.PathLike[str]) -> str:
        real = os.path.realpath(str(path))
        try:
            inside = os.path.commonpath([str(self.root), real]) == str(self.root)
        except ValueError:
            inside = False
        if not inside:
            raise LibraryError("La ruta sale de la biblioteca")
        rel = os.path.relpath(real, self.root)
        return "" if rel == "." else Path(rel).as_posix()

    def require_file(self, rel: str, extensions: set[str] | None = None) -> Path:
        path = self.resolve(rel)
        if not path.is_file():
            raise LibraryError(f"No existe el archivo: {rel}")
        if extensions is not None and path.suffix.lower() not in extensions:
            raise LibraryError(f"Extensión no permitida: {path.suffix or '(sin extensión)'}")
        return path

    def validate_delete(self, rel: str) -> str:
        """Return the canonical relative path for a safe deletion."""
        text = (rel or "").strip()
        if not text:
            raise LibraryError("No se puede eliminar la biblioteca")
        raw = Path(os.path.abspath(os.path.join(self.root, text)))
        if raw.is_symlink():
            raise LibraryError("No se pueden eliminar enlaces simbólicos")
        path = self.resolve(text)
        if path == self.root:
            raise LibraryError("No se puede eliminar la biblioteca")
        if not path.exists():
            raise LibraryError(f"No existe: {text}")
        return self.relpath(path)

    def delete(self, rel: str) -> str:
        canonical = self.validate_delete(rel)
        path = self.resolve(canonical)
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            raise LibraryError(f"No se pudo eliminar {rel}: {exc}") from exc
        self.invalidate(canonical)
        return canonical

    # ---------- metadata ----------

    def pkg_info(self, rel_or_path: str | os.PathLike[str]) -> PkgInfo:
        path = self._resolve_any(rel_or_path)
        try:
            stat = path.stat()
        except OSError as exc:
            raise LibraryError(f"No se puede leer {path}: {exc}") from exc
        key = str(path)
        cache_key = (stat.st_size, stat.st_mtime_ns)
        with self._lock:
            cached = self._pkg_cache.get(key)
            if cached and cached[0] == cache_key[0] and cached[1] == cache_key[1]:
                self._pkg_cache.move_to_end(key)
                return cached[2]
        try:
            info = parse_pkg(path)
        except PkgError as exc:
            raise LibraryError(str(exc)) from exc
        with self._lock:
            self._pkg_cache[key] = (cache_key[0], cache_key[1], info)
            self._pkg_cache.move_to_end(key)
            while len(self._pkg_cache) > self._cache_limit:
                self._pkg_cache.popitem(last=False)
        return info

    def icon(self, rel_or_path: str | os.PathLike[str]) -> bytes | None:
        path = self._resolve_any(rel_or_path)
        if path.suffix.lower() != ".pkg" or not path.is_file():
            return None
        key = icon_cache_key(path)
        if not key:
            return None
        cache_path = self.icons_dir / f"{key}.png"
        try:
            if cache_path.is_file() and cache_path.stat().st_size > 8:
                return cache_path.read_bytes()
        except OSError:
            pass
        data = extract_icon(path)
        if not data:
            return None
        try:
            self.icons_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, cache_path)
        except OSError:
            pass
        return data

    def invalidate(self, rel: str | None = None) -> None:
        prefix = str(self.resolve(rel)) if rel else None
        with self._lock:
            if prefix is None:
                self._pkg_cache.clear()
                return
            for key in list(self._pkg_cache):
                if key == prefix or key.startswith(prefix.rstrip("/") + "/"):
                    self._pkg_cache.pop(key, None)

    # ---------- listing ----------

    def _entry_for(self, path: Path, rel: str) -> dict[str, Any] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        base = {
            "name": path.name,
            "path": rel,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "size_human": human_size(stat.st_size),
        }
        if path.is_dir():
            base["type"] = "dir"
            base["pkg_count"] = self._count_pkgs(path)
            return base
        suffix = path.suffix.lower()
        if suffix in PKG_EXTENSIONS:
            base["type"] = "pkg"
            base["icon_url"] = "/api/icon?" + urlencode({"path": rel})
            try:
                info = self.pkg_info(path)
            except LibraryError as exc:
                base["metadata_error"] = str(exc)
                base["title"] = path.stem
                base["kind"] = "invalid"
                base["firmware"] = ""
                base["title_id"] = ""
                return base
            base.update(
                {
                    "title": info.title,
                    "title_id": info.title_id,
                    "content_id": info.content_id,
                    "version": info.version,
                    "app_ver": info.app_ver,
                    "category": info.category,
                    "kind": info.kind,
                    "firmware": info.firmware,
                    "system_ver": info.system_ver,
                    "icon_available": info.icon_available,
                }
            )
            return base
        if suffix in ARCHIVE_EXTENSIONS:
            base["type"] = "rar" if suffix == ".rar" else "archive"
            base["archive_ext"] = suffix
            return base
        base["type"] = "file"
        return base

    @staticmethod
    def _count_pkgs(path: Path) -> int:
        count = 0
        try:
            with os.scandir(path) as iterator:
                for entry in iterator:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() == ".pkg":
                            count += 1
                    except OSError:
                        continue
        except OSError:
            return 0
        return count

    def listing(self, rel: str | None = "") -> dict[str, Any]:
        path = self.resolve(rel)
        if not path.exists():
            raise LibraryError(f"No existe la carpeta: {rel or '/'}")
        if not path.is_dir():
            raise LibraryError(f"No es una carpeta: {rel}")
        entries: list[dict[str, Any]] = []
        try:
            with os.scandir(path) as iterator:
                for item in iterator:
                    if item.name.startswith("."):
                        continue
                    rel_item = self.relpath(Path(item.path))
                    entry = self._entry_for(Path(item.path), rel_item)
                    if entry is not None:
                        entries.append(entry)
        except OSError as exc:
            raise LibraryError(f"No se puede leer la carpeta: {exc}") from exc
        entries.sort(key=lambda item: (item["type"] != "dir", item.get("title", item["name"]).casefold(), item["name"].casefold()))
        current_rel = self.relpath(path)
        breadcrumb: list[dict[str, str]] = [{"name": "Biblioteca", "path": ""}]
        if current_rel:
            accumulated = []
            for part in current_rel.split("/"):
                accumulated.append(part)
                breadcrumb.append({"name": part, "path": "/".join(accumulated)})
        parent = None
        if current_rel:
            parent_path = Path(current_rel).parent.as_posix()
            parent = "" if parent_path == "." else parent_path
        return {
            "path": current_rel,
            "parent": parent,
            "breadcrumb": breadcrumb,
            "entries": entries,
            "disk": self.disk_stats(),
            "root": str(self.root),
        }

    def disk_stats(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.root)
        except OSError:
            usage = shutil.disk_usage(self.root.parent)
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "total_human": human_size(usage.total),
            "used_human": human_size(usage.used),
            "free_human": human_size(usage.free),
            "percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
        }

    # ---------- recursive scan ----------

    def scan_packages(self, rel: str) -> tuple[list[PkgInfo], list[str]]:
        path = self.resolve(rel)
        if path.is_file():
            if path.suffix.lower() not in PKG_EXTENSIONS:
                return [], [f"No es un PKG: {path.name}"]
            try:
                return [self.pkg_info(path)], []
            except LibraryError as exc:
                return [], [f"{path.name}: {exc}"]
        if not path.is_dir():
            raise LibraryError(f"No existe la ruta: {rel}")
        infos: list[PkgInfo] = []
        errors: list[str] = []
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            dirnames[:] = sorted(
                [name for name in dirnames if not name.startswith(".")],
                key=str.casefold,
            )
            for filename in sorted(filenames, key=str.casefold):
                if not filename.lower().endswith(".pkg"):
                    continue
                full = Path(dirpath) / filename
                try:
                    infos.append(self.pkg_info(full))
                except LibraryError as exc:
                    errors.append(f"{self.relpath(full)}: {exc}")
        return sort_packages(infos), errors

    def find_packages(self, rel: str) -> list[PkgInfo]:
        return self.scan_packages(rel)[0]

    def archive_info(self, rel: str) -> dict[str, Any]:
        path = self.require_file(rel, ARCHIVE_EXTENSIONS)
        return {
            "path": self.relpath(path),
            "name": path.name,
            "size": path.stat().st_size,
            "size_human": human_size(path.stat().st_size),
            "extension": path.suffix.lower(),
        }

    # ---------- URLs ----------

    def pkg_url(self, rel: str, base_url: str) -> str:
        path = self.require_file(rel, PKG_EXTENSIONS)
        rel_path = self.relpath(path)
        quoted = quote(rel_path, safe="/")
        return f"{base_url.rstrip('/')}/{quoted}"

    @staticmethod
    def icon_url(rel: str) -> str:
        return "/api/icon?" + urlencode({"path": rel})

    def icon_etag(self, rel: str) -> str:
        path = self.require_file(rel, PKG_EXTENSIONS)
        key = icon_cache_key(path)
        return '"' + hashlib.sha1(key.encode()).hexdigest()[:16] + '"'
