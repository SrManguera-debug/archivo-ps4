"""Persistent install/extract queue and its workers."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .consoles import Console, ConsoleError, ConsoleStore
from .library import Library, LibraryError
from .pkg import (
    PkgInfo,
    firmware_satisfied,
    human_size,
    sort_packages,
)
from .rpi import RpiClient, RpiError, extract_task_ids, progress_snapshot

LOGGER = logging.getLogger(__name__)

ACTIVE_STATES = {"queued", "starting", "running", "paused", "extracting"}
TERMINAL_STATES = {"done", "error", "cancelled"}
INSTALL_STATES = {"starting", "running", "paused"}
JOB_HISTORY_LIMIT = 200


class JobError(RuntimeError):
    """A job cannot be created or operated on."""


def _now() -> float:
    return time.time()


@dataclass
class Job:
    id: str
    type: str
    state: str = "queued"
    console_id: str = ""
    console_name: str = ""
    package_path: str = ""
    url: str = ""
    title: str = ""
    title_id: str = ""
    kind: str = ""
    size: int = 0
    task_id: int | None = None
    progress: float = 0.0
    phase: str = "queued"
    message: str = "En cola"
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    started_at: float | None = None
    finished_at: float | None = None
    archive_path: str = ""
    dest_path: str = ""
    delete_archive: bool = False
    overwrite: bool = False
    rpi: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["size_human"] = human_size(self.size)
        data["type_label"] = "Instalación" if self.type == "install" else "Extracción"
        data["can_pause"] = self.type == "install" and self.state in ("starting", "running")
        data["can_resume"] = self.type == "install" and self.state == "paused"
        data["can_cancel"] = self.state in ACTIVE_STATES
        data["can_retry"] = self.state in TERMINAL_STATES and self.state != "done"
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        fields = {
            "id": str(data.get("id") or uuid.uuid4().hex),
            "type": str(data.get("type") or "install"),
            "state": str(data.get("state") or "queued"),
            "console_id": str(data.get("console_id") or ""),
            "console_name": str(data.get("console_name") or ""),
            "package_path": str(data.get("package_path") or ""),
            "url": str(data.get("url") or ""),
            "title": str(data.get("title") or ""),
            "title_id": str(data.get("title_id") or ""),
            "kind": str(data.get("kind") or ""),
            "size": int(data.get("size") or 0),
            "task_id": (
                int(data["task_id"])
                if data.get("task_id") is not None
                else None
            ),
            "progress": float(data.get("progress") or 0.0),
            "phase": str(data.get("phase") or "queued"),
            "message": str(data.get("message") or ""),
            "error": str(data["error"]) if data.get("error") else None,
            "warnings": [str(item) for item in (data.get("warnings") or [])],
            "created_at": float(data.get("created_at") or _now()),
            "updated_at": float(data.get("updated_at") or _now()),
            "started_at": (
                float(data["started_at"]) if data.get("started_at") else None
            ),
            "finished_at": (
                float(data["finished_at"]) if data.get("finished_at") else None
            ),
            "archive_path": str(data.get("archive_path") or ""),
            "dest_path": str(data.get("dest_path") or ""),
            "delete_archive": bool(data.get("delete_archive") or False),
            "overwrite": bool(data.get("overwrite") or False),
            "rpi": data.get("rpi") if isinstance(data.get("rpi"), dict) else None,
        }
        return cls(**fields)


class JobStore:
    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.path = Path(state_dir) / "jobs.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def load(self) -> list[Job]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("jobs", []) if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                raise ValueError("jobs must be a list")
            return [Job.from_dict(item) for item in items if isinstance(item, dict)]
        except Exception as exc:
            backup = self.path.with_suffix(f".corrupt-{int(time.time())}")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            LOGGER.error("jobs.json inválido (%s); respaldado en %s", exc, backup)
            return []

    def save(self, jobs: Iterable[Job]) -> None:
        payload = {"jobs": [job.to_dict() for job in jobs]}
        with self._lock:
            fd, tmp = tempfile.mkstemp(
                prefix=".jobs-", suffix=".tmp", dir=str(self.path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True)
                    fh.write("\n")
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise


def parse_7z_listing(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("Path = "):
            if current is not None:
                entries.append(current)
            current = {"path": line[7:].strip(), "size": 0, "is_dir": False}
        elif current is not None and line.startswith("Size = "):
            try:
                current["size"] = int(line[7:].strip())
            except ValueError:
                current["size"] = 0
        elif current is not None and line.startswith("Folder = "):
            current["is_dir"] = line[9:].strip() == "+"
        elif current is not None and line.startswith("Type = "):
            current["type"] = line[7:].strip()
    if current is not None:
        entries.append(current)
    return entries


def _paths_overlap(left: str, right: str) -> bool:
    left = str(left or "").strip("/")
    right = str(right or "").strip("/")
    if not left or not right:
        return False
    return (
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


def _safe_archive_member(path: str) -> str:
    value = (path or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    if not value:
        raise JobError("El archivo contiene una entrada sin nombre")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise JobError(f"Entrada con ruta absoluta: {path}")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise JobError(f"Entrada con '..': {path}")
    if not parts:
        raise JobError(f"Entrada inválida: {path}")
    return "/".join(parts)


def _fmt_eta(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


class JobManager:
    def __init__(
        self,
        config: Config,
        console_store: ConsoleStore,
        library: Library,
        *,
        poll_interval: float = 2.0,
        finish_grace: float = 300.0,
        done_grace: float = 2.0,
        stall_timeout: float = 900.0,
        max_install_seconds: float = 24 * 3600,
        seven_zip: str | None = None,
        start_workers: bool = True,
    ) -> None:
        self.config = config
        self.console_store = console_store
        self.library = library
        self.poll_interval = max(0.02, poll_interval)
        self.finish_grace = max(0.0, finish_grace)
        self.done_grace = max(0.0, done_grace)
        self.stall_timeout = max(1.0, stall_timeout)
        self.max_install_seconds = max(60.0, max_install_seconds)
        self.seven_zip = seven_zip or shutil.which("7z") or shutil.which("7zz") or "7z"
        self._start_workers = start_workers
        self._store = JobStore(config.state_dir)
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._queues: dict[str, queue.Queue[str | None]] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._enqueued: set[str] = set()
        self._extract_queue: queue.Queue[str | None] = queue.Queue()
        self._extract_worker: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._cancel_requested: set[str] = set()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._load()
        self._mark_interrupted()
        self._persist()
        if start_workers:
            self._bootstrap()

    # ---------- lifecycle ----------

    def _load(self) -> None:
        for job in self._store.load():
            self._jobs[job.id] = job

    def _mark_interrupted(self) -> None:
        for job in self._jobs.values():
            if job.state in ("starting", "running", "paused", "extracting"):
                job.state = "error"
                job.phase = "error"
                job.error = "Interrumpido por reinicio del servicio"
                job.message = "Interrumpido por reinicio; podés reintentar"
                job.finished_at = _now()
                job.updated_at = _now()

    def _bootstrap(self) -> None:
        with self._lock:
            queued = [job for job in self._jobs.values() if job.state == "queued"]
        for job in sorted(queued, key=lambda item: item.created_at):
            if job.type == "install":
                self._ensure_worker(job.console_id)
                self._put_install(job)
            else:
                self._ensure_extract_worker()
                self._put_extract(job)

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._lock:
            console_ids = list(self._queues)
            for job_id, proc in list(self._processes.items()):
                if job_id in self._cancel_requested:
                    continue
                try:
                    proc.terminate()
                except OSError:
                    pass
        for console_id in console_ids:
            try:
                self._queues[console_id].put_nowait(None)
            except queue.Full:
                pass
        try:
            self._extract_queue.put_nowait(None)
        except queue.Full:
            pass
        for thread in list(self._workers.values()) + (
            [self._extract_worker] if self._extract_worker else []
        ):
            thread.join(timeout=2.0)
        self._persist()

    def _persist(self) -> None:
        with self._lock:
            jobs = self._prune_locked()
            self._store.save(jobs)

    def _prune_locked(self) -> list[Job]:
        jobs = list(self._jobs.values())
        if len(jobs) <= JOB_HISTORY_LIMIT:
            return sorted(jobs, key=lambda item: item.created_at)
        terminal = sorted(
            (job for job in jobs if job.state in TERMINAL_STATES),
            key=lambda item: item.created_at,
        )
        remove_count = len(jobs) - JOB_HISTORY_LIMIT
        for job in terminal[:remove_count]:
            self._jobs.pop(job.id, None)
        return sorted(self._jobs.values(), key=lambda item: item.created_at)

    # ---------- queries ----------

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobError(f"Trabajo no encontrado: {job_id}")
            return job

    def list(self, console_id: str | None = None) -> list[Job]:
        with self._lock:
            jobs = [
                job
                for job in self._jobs.values()
                if not console_id or job.console_id == console_id
            ]
        return sorted(jobs, key=lambda item: item.created_at, reverse=True)

    def active_for_console(self, console_id: str) -> list[Job]:
        return [job for job in self.list(console_id) if job.state in ACTIVE_STATES]

    def has_active_jobs(self, console_id: str) -> bool:
        return bool(self.active_for_console(console_id))

    def clear_terminal(self, console_id: str | None = None) -> int:
        """Remove finished, failed and cancelled jobs, keeping active work."""
        with self._lock:
            remove_ids = [
                job_id
                for job_id, job in self._jobs.items()
                if job.state in TERMINAL_STATES
                and (not console_id or job.console_id == console_id)
            ]
            for job_id in remove_ids:
                self._jobs.pop(job_id, None)
            if remove_ids:
                self._persist_locked()
        return len(remove_ids)

    def conflicting_jobs(self, paths: Iterable[str]) -> list[Job]:
        normalized = [
            str(path).strip("/") for path in paths if str(path or "").strip("/")
        ]
        if not normalized:
            return []
        with self._lock:
            active = [
                job for job in self._jobs.values() if job.state in ACTIVE_STATES
            ]
        conflicts: list[Job] = []
        for job in active:
            protected = (job.archive_path, job.package_path, job.dest_path)
            if any(
                _paths_overlap(path, item)
                for path in normalized
                for item in protected
                if item
            ):
                conflicts.append(job)
        return conflicts

    # ---------- creation ----------

    def _collect_packages(
        self, files: list[str], include_dlc: bool
    ) -> tuple[list[PkgInfo], list[str]]:
        infos: list[PkgInfo] = []
        warnings: list[str] = []
        seen: set[str] = set()
        for raw in files:
            rel = str(raw or "").strip()
            if not rel:
                continue
            path = self.library.resolve(rel)
            if path.is_dir():
                found, errors = self.library.scan_packages(rel)
                warnings.extend(errors)
                for info in found:
                    if info.kind == "dlc" and not include_dlc:
                        continue
                    if info.path not in seen:
                        seen.add(info.path)
                        infos.append(info)
            elif path.is_file():
                try:
                    info = self.library.pkg_info(rel)
                except LibraryError as exc:
                    warnings.append(f"{rel}: {exc}")
                    continue
                if info.path not in seen:
                    seen.add(info.path)
                    infos.append(info)
            else:
                warnings.append(f"No existe: {rel}")
        return sort_packages(infos), warnings

    def create_install_batch(
        self, console_id: str, files: list[str], include_dlc: bool = False
    ) -> tuple[list[Job], list[str]]:
        try:
            console = self.console_store.require(console_id)
        except ConsoleError as exc:
            raise JobError(str(exc)) from exc
        if not isinstance(files, list) or not files:
            raise JobError("No se seleccionaron paquetes")
        infos, warnings = self._collect_packages(files[:500], include_dlc)
        if not infos:
            raise JobError("No se encontraron PKGs válidos en la selección")
        created: list[Job] = []
        with self._lock:
            active_paths = {
                (job.console_id, job.package_path)
                for job in self._jobs.values()
                if job.state in ACTIVE_STATES and job.type == "install"
            }
            for info in infos:
                try:
                    rel = self.library.relpath(info.path)
                    url = self.library.pkg_url(rel, self.config.public_base_url)
                except LibraryError as exc:
                    warnings.append(f"{info.path}: {exc}")
                    continue
                key = (console_id, rel)
                if key in active_paths:
                    warnings.append(f"Ya está en cola: {info.title or rel}")
                    continue
                warning_list: list[str] = []
                satisfied = firmware_satisfied(info.firmware, console.firmware)
                if satisfied is False:
                    warning_list.append(
                        f"Requiere firmware {info.firmware}; la consola figura en {console.firmware}"
                    )
                job = Job(
                    id=uuid.uuid4().hex,
                    type="install",
                    state="queued",
                    console_id=console.id,
                    console_name=console.name,
                    package_path=rel,
                    url=url,
                    title=info.title or os.path.basename(rel),
                    title_id=info.title_id,
                    kind=info.kind,
                    size=info.size,
                    message="En cola",
                    warnings=warning_list,
                )
                self._jobs[job.id] = job
                active_paths.add(key)
                created.append(job)
            self._persist_locked()
        for job in created:
            self._ensure_worker(job.console_id)
            self._put_install(job)
        return created, warnings

    def create_extract(
        self,
        archive_rel: str,
        delete_archive: bool = False,
        overwrite: bool = False,
    ) -> Job:
        try:
            info = self.library.archive_info(archive_rel)
        except LibraryError as exc:
            raise JobError(str(exc)) from exc
        rel = info["path"]
        with self._lock:
            for job in self._jobs.values():
                if (
                    job.type == "extract"
                    and job.archive_path == rel
                    and job.state in ACTIVE_STATES
                ):
                    raise JobError("Ese archivo ya tiene una extracción en curso")
            job = Job(
                id=uuid.uuid4().hex,
                type="extract",
                state="queued",
                package_path=rel,
                archive_path=rel,
                title=info["name"],
                size=info["size"],
                message="En cola para extraer",
                delete_archive=bool(delete_archive),
                overwrite=bool(overwrite),
            )
            self._jobs[job.id] = job
            self._persist_locked()
        self._ensure_extract_worker()
        self._put_extract(job)
        return job

    # ---------- queue plumbing ----------

    def _ensure_worker(self, console_id: str) -> None:
        if not self._start_workers:
            return
        with self._lock:
            if console_id in self._workers and self._workers[console_id].is_alive():
                return
            if not console_id:
                return
            self._queues.setdefault(console_id, queue.Queue())
            thread = threading.Thread(
                target=self._console_worker,
                args=(console_id,),
                name=f"install-{console_id[:8]}",
                daemon=True,
            )
            self._workers[console_id] = thread
            thread.start()

    def _put_install(self, job: Job) -> None:
        with self._lock:
            if job.id in self._enqueued:
                return
            self._enqueued.add(job.id)
            self._queues.setdefault(job.console_id, queue.Queue()).put(job.id)

    def _ensure_extract_worker(self) -> None:
        if not self._start_workers:
            return
        with self._lock:
            if self._extract_worker and self._extract_worker.is_alive():
                return
            self._extract_worker = threading.Thread(
                target=self._extract_worker_loop,
                name="extract-worker",
                daemon=True,
            )
            self._extract_worker.start()

    def _put_extract(self, job: Job) -> None:
        with self._lock:
            if job.id in self._enqueued:
                return
            self._enqueued.add(job.id)
            self._extract_queue.put(job.id)

    def _persist_locked(self) -> None:
        self._store.save(self._prune_locked())

    # ---------- helpers ----------

    def _update(self, job: Job, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = _now()
            self._persist_locked()

    def _update_if_active(self, job: Job, **fields: Any) -> bool:
        """Update progress without stomping on a pause/cancel from the API."""
        with self._lock:
            if job.id in self._cancel_requested or job.state in ("paused", "cancelled"):
                return False
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = _now()
            self._persist_locked()
            return True

    def _finish(
        self,
        job: Job,
        state: str,
        progress: float,
        phase: str,
        message: str,
        error: str | None = None,
    ) -> None:
        with self._lock:
            if job.id in self._cancel_requested and job.state == "cancelled":
                return
            job.state = state
            job.progress = round(max(0.0, min(100.0, progress)), 2)
            job.phase = phase
            job.message = message
            job.error = error
            job.finished_at = _now()
            job.updated_at = _now()
            self._enqueued.discard(job.id)
            self._persist_locked()

    def _sleep(self, seconds: float) -> bool:
        """Sleep in small slices; False when shutdown was requested."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._shutdown.is_set():
                return False
            time.sleep(min(0.1, max(0.0, deadline - time.time())))
        return not self._shutdown.is_set()

    def _wait_while_paused(self, job: Job) -> bool:
        while not self._shutdown.is_set():
            with self._lock:
                if job.state != "paused":
                    return True
                if job.id in self._cancel_requested:
                    return False
            time.sleep(0.1)
        return False

    def _client(self, console: Console) -> RpiClient:
        return RpiClient(console.ip, console.rpi_port, timeout=15)

    def _async_rpi(self, job: Job, action: str) -> None:
        task_id = job.task_id
        console = self.console_store.get(job.console_id)
        if task_id is None or console is None:
            return

        def run() -> None:
            try:
                client = self._client(console)
                getattr(client, action)(int(task_id))
            except RpiError as exc:
                LOGGER.warning("RPI %s falló para %s: %s", action, job.id, exc)
                with self._lock:
                    if job.state in ("paused", "cancelled"):
                        job.message = f"{job.message} (RPI no confirmó: {exc})"
                        job.updated_at = _now()
                        self._persist_locked()

        threading.Thread(target=run, name=f"rpi-{action}", daemon=True).start()

    # ---------- operations ----------

    def pause(self, job_id: str) -> Job:
        with self._lock:
            job = self.get(job_id)
            if job.type != "install":
                raise JobError("Solo se pueden pausar instalaciones")
            if job.state not in ("starting", "running"):
                raise JobError(f"No se puede pausar un trabajo en estado {job.state}")
            job.state = "paused"
            job.phase = "paused"
            job.message = "Pausado"
            job.updated_at = _now()
            self._persist_locked()
        self._async_rpi(job, "pause")
        return job

    def resume(self, job_id: str) -> Job:
        with self._lock:
            job = self.get(job_id)
            if job.type != "install":
                raise JobError("Solo se pueden reanudar instalaciones")
            if job.state != "paused":
                raise JobError("El trabajo no está pausado")
            job.state = "running"
            job.phase = "downloading"
            job.message = "Reanudando"
            job.updated_at = _now()
            self._persist_locked()
        self._async_rpi(job, "resume")
        return job

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            job = self.get(job_id)
            if job.state not in ACTIVE_STATES:
                raise JobError("El trabajo ya terminó")
            self._cancel_requested.add(job.id)
            was_extracting = job.state == "extracting"
            job.state = "cancelled"
            job.phase = "cancelled"
            job.message = "Cancelado"
            job.error = None
            job.finished_at = _now()
            job.updated_at = _now()
            proc = self._processes.get(job.id)
            self._persist_locked()
        if job.type == "install" and job.task_id is not None:
            self._async_rpi(job, "stop")
        if was_extracting and proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        return job

    def retry(self, job_id: str) -> Job:
        with self._lock:
            job = self.get(job_id)
            if job.state not in ("error", "cancelled"):
                raise JobError("Solo se pueden reintentar trabajos fallidos o cancelados")
            active_paths = {
                (item.console_id, item.package_path)
                for item in self._jobs.values()
                if item.state in ACTIVE_STATES and item.id != job.id and item.type == "install"
            }
            if job.type == "install" and (job.console_id, job.package_path) in active_paths:
                raise JobError("Ya hay un trabajo activo para ese paquete")
            job.state = "queued"
            job.phase = "queued"
            job.progress = 0.0
            job.error = None
            job.message = "En cola"
            job.task_id = None
            job.rpi = None
            job.started_at = None
            job.finished_at = None
            job.updated_at = _now()
            self._cancel_requested.discard(job.id)
            self._enqueued.discard(job.id)
            self._persist_locked()
        if job.type == "install":
            self._ensure_worker(job.console_id)
            self._put_install(job)
        else:
            self._ensure_extract_worker()
            self._put_extract(job)
        return job

    # ---------- install worker ----------

    def _console_worker(self, console_id: str) -> None:
        q = self._queues[console_id]
        while not self._shutdown.is_set():
            try:
                job_id = q.get(timeout=1.0)
            except queue.Empty:
                continue
            if job_id is None:
                break
            with self._lock:
                self._enqueued.discard(job_id)
                job = self._jobs.get(job_id)
            if job is None or job.state != "queued":
                continue
            try:
                self._run_install(job)
            except Exception as exc:
                LOGGER.exception("Fallo inesperado en job %s", job_id)
                self._finish(job, "error", job.progress, "error", "Error inesperado", str(exc))

    def _run_install(self, job: Job) -> None:
        console = self.console_store.get(job.console_id)
        if console is None:
            self._finish(job, "error", 0, "error", "Consola no encontrada", "Consola eliminada")
            return
        self._update(
            job,
            state="starting",
            phase="preparing",
            message="Conectando con la consola",
            started_at=_now(),
            error=None,
        )
        if not self._wait_while_paused(job):
            if job.state == "paused":
                self._finish(job, "cancelled", job.progress, "cancelled", "Cancelado")
            return
        if job.id in self._cancel_requested:
            return
        client = self._client(console)
        response: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(3):
            if self._shutdown.is_set() or job.id in self._cancel_requested:
                return
            try:
                response = client.install([job.url])
                break
            except RpiError as exc:
                last_error = exc
                if attempt < 2:
                    self._update(
                        job,
                        message=f"Reintentando conexión ({attempt + 1}/3)",
                    )
                    if not self._sleep(1.0 + attempt):
                        return
        if response is None:
            self._finish(
                job,
                "error",
                job.progress,
                "error",
                "No se pudo iniciar en la consola",
                str(last_error or "RPI no respondió"),
            )
            return
        task_ids = extract_task_ids(response)
        if not task_ids:
            self._finish(
                job,
                "error",
                0,
                "error",
                "RPI no devolvió task_id",
                json.dumps(response, ensure_ascii=False)[:1000],
            )
            return
        task_id = task_ids[0]
        with self._lock:
            if job.id in self._cancel_requested:
                return
            job.task_id = task_id
            if job.state == "paused":
                job.phase = "paused"
                job.message = "Pausado"
            else:
                job.state = "running"
                job.phase = "downloading"
                job.message = "Descargando en la consola"
            job.rpi = response
            job.updated_at = _now()
            self._persist_locked()
            paused_now = job.state == "paused"
        if paused_now:
            try:
                client.pause(task_id)
            except RpiError as exc:
                LOGGER.warning("No se pudo pausar %s: %s", job.id, exc)
        self._poll_install(job, console, client, task_id)

    def _poll_install(
        self, job: Job, console: Console, client: RpiClient, task_id: int
    ) -> None:
        started = job.started_at or _now()
        consecutive_errors = 0
        last_change = _now()
        last_signature: tuple[Any, ...] | None = None
        finish_since: float | None = None
        done_since: float | None = None
        while not self._shutdown.is_set():
            if job.id in self._cancel_requested:
                return
            if job.state == "paused":
                if not self._wait_while_paused(job):
                    return
                last_change = _now()
                continue
            if _now() - started > self.max_install_seconds:
                self._finish(
                    job,
                    "error",
                    job.progress,
                    "error",
                    "Tiempo máximo excedido",
                    "La instalación superó el tiempo máximo permitido",
                )
                return
            try:
                response = client.progress(task_id)
            except RpiError as exc:
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    self._finish(
                        job,
                        "error",
                        job.progress,
                        "error",
                        "Se perdió la conexión con la consola",
                        str(exc),
                    )
                    return
                self._update(
                    job,
                    message=f"Sin respuesta de RPI ({consecutive_errors}/10)",
                )
                if not self._sleep(self.poll_interval):
                    return
                continue
            consecutive_errors = 0
            snap = progress_snapshot(response)
            if snap["error"]:
                self._finish(
                    job,
                    "error",
                    snap["percent"],
                    "error",
                    "La consola reportó un error",
                    str(snap["error"]),
                )
                return
            signature = (
                snap["transferred"],
                snap["local_copy_percent"],
                snap["phase"],
                snap["percent"],
            )
            if signature != last_signature:
                last_signature = signature
                last_change = _now()
            if snap["done"]:
                # RPI can briefly return stale done progress from a previous
                # task. Confirm it for a couple of polls before trusting it.
                if done_since is None:
                    done_since = _now()
                    self._update(
                        job,
                        message="Confirmando instalación con la consola",
                    )
                if _now() - done_since >= self.done_grace:
                    self._finish(
                        job,
                        "done",
                        100,
                        "done",
                        f"Instalado en {console.name}",
                        None,
                    )
                    return
                if not self._sleep(self.poll_interval):
                    return
                continue
            done_since = None
            if snap["transfer_done"]:
                if snap["local_copy_percent"] < 0:
                    finish_since = finish_since or _now()
                    message = "Transferencia completa; esperando confirmación de la consola"
                    phase = "finishing"
                    progress = 100.0
                    if _now() - finish_since >= self.finish_grace:
                        self._finish(
                            job,
                            "done",
                            100,
                            "done",
                            "Transferencia completa (RPI no reportó el cierre)",
                            None,
                        )
                        return
                else:
                    finish_since = None
                    phase = "installing"
                    progress = float(snap["local_copy_percent"])
                    message = f"Instalando · {progress:.1f}%"
                    if _now() - last_change > self.stall_timeout:
                        self._finish(
                            job,
                            "error",
                            progress,
                            "error",
                            "La instalación dejó de avanzar",
                            "Sin progreso de instalación durante demasiado tiempo",
                        )
                        return
            else:
                finish_since = None
                phase = snap["phase"] or "downloading"
                progress = float(snap["percent"])
                eta = _fmt_eta(snap.get("eta"))
                transferred = human_size(snap["transferred"])
                total = human_size(snap["total"]) if snap["total"] else "?"
                message = f"{transferred} / {total} · {progress:.1f}%"
                if eta:
                    message += f" · ETA {eta}"
            if not self._update_if_active(
                job,
                state="running",
                phase=phase,
                progress=progress,
                message=message,
                rpi={
                    "transferred": snap["transferred"],
                    "total": snap["total"],
                    "transfer_percent": snap["transfer_percent"],
                    "local_copy_percent": snap["local_copy_percent"],
                    "eta": snap["eta"],
                },
            ):
                if job.id in self._cancel_requested:
                    return
            if not self._sleep(self.poll_interval):
                return

    # ---------- extraction worker ----------

    def _extract_worker_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                job_id = self._extract_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if job_id is None:
                break
            with self._lock:
                self._enqueued.discard(job_id)
                job = self._jobs.get(job_id)
            if job is None or job.state != "queued":
                continue
            try:
                self._run_extract(job)
            except Exception as exc:
                LOGGER.exception("Fallo inesperado extrayendo %s", job_id)
                self._finish(job, "error", job.progress, "error", "Error extrayendo", str(exc))
            finally:
                with self._lock:
                    self._processes.pop(job.id, None)

    def _inspect_archive(self, archive: Path) -> tuple[list[dict[str, Any]], int, str, Path]:
        try:
            result = subprocess.run(
                [self.seven_zip, "l", "-slt", str(archive)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except FileNotFoundError as exc:
            raise JobError(
                "No está instalado 7z. Instalá p7zip-full en el servidor."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise JobError("7z tardó demasiado en leer el archivo") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            raise JobError(f"7z no pudo leer el archivo: {detail}")
        raw_entries = parse_7z_listing(result.stdout)
        if not raw_entries:
            raise JobError("7z no devolvió entradas para el archivo")
        entries: list[dict[str, Any]] = []
        total_size = 0
        top_levels: set[str] = set()
        for index, entry in enumerate(raw_entries):
            raw_path = str(entry.get("path", ""))
            if index == 0 and (
                entry.get("type") or Path(raw_path).name == archive.name
            ):
                continue  # archive metadata block
            safe = _safe_archive_member(raw_path)
            size = max(0, int(entry.get("size") or 0))
            is_dir = bool(entry.get("is_dir"))
            entries.append({"path": safe, "size": size, "is_dir": is_dir})
            if not is_dir:
                total_size += size
            top_levels.add(safe.split("/", 1)[0])
        if not entries:
            raise JobError("El archivo no contiene entradas")
        dest = archive.parent
        if not top_levels:
            raise JobError("No se pudo determinar el contenido del archivo")
        existing = [name for name in sorted(top_levels) if (dest / name).exists()]
        if existing:
            raise JobError(
                "Ya existe en el destino: "
                + ", ".join(existing[:5])
                + ". Marcá 'sobrescribir' para continuar."
            )
        free = shutil.disk_usage(dest).free
        margin = 1024**3
        if total_size and free < total_size + margin:
            raise JobError(
                f"Espacio insuficiente: se necesitan {human_size(total_size)} "
                f"y hay {human_size(free)} libres (margen de 1 GiB)."
            )
        return entries, total_size, "", dest

    def _run_extract(self, job: Job) -> None:
        archive = self.library.require_file(job.archive_path)
        if not os.access(self.seven_zip, os.X_OK):
            raise JobError(f"No se encontró el ejecutable 7z: {self.seven_zip}")
        self._update(
            job,
            state="extracting",
            phase="preparing",
            progress=0.0,
            message="Analizando el archivo",
            started_at=_now(),
            error=None,
        )
        try:
            entries, total_size, _, dest = self._inspect_archive(archive)
        except JobError as exc:
            if not job.overwrite:
                self._finish(job, "error", 0, "error", "No se puede extraer", str(exc))
                return
            # The overwrite flag only bypasses the existing-target check.
            try:
                entries, total_size, _, dest = self._inspect_archive_allow_existing(archive)
            except JobError as retry_exc:
                self._finish(job, "error", 0, "error", "No se puede extraer", str(retry_exc))
                return
        dest_rel = self.library.relpath(dest)
        self._update(
            job,
            dest_path=dest_rel,
            phase="extracting",
            message=f"Extrayendo {human_size(total_size)}",
            progress=0.0,
        )
        cmd = [
            self.seven_zip,
            "x",
            "-y",
            "-bsp1",
            f"-o{dest}",
            str(archive),
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            self._finish(job, "error", 0, "error", "No está instalado 7z", str(exc))
            return
        with self._lock:
            self._processes[job.id] = proc
        last_lines: list[str] = []
        last_update = 0.0
        percent = 0.0
        parse_tail = ""
        assert proc.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                if job.id in self._cancel_requested:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                    break
                if not selector.select(timeout=0.25):
                    continue
                chunk = os.read(proc.stdout.fileno(), 8192)
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                # 7z -bsp1 redraws the percentage with backspaces instead of
                # ending each update with a newline. Read chunks, not lines.
                parse_tail = (parse_tail + text)[-256:]
                matches = re.findall(r"(\d{1,3})%", parse_tail)
                if matches:
                    percent = max(percent, min(100.0, float(matches[-1])))
                    if _now() - last_update >= 0.2:
                        last_update = _now()
                        self._update(
                            job,
                            progress=percent,
                            message=f"Extrayendo · {percent:.0f}%",
                        )
                for raw_line in re.split(r"[\r\n]+", text):
                    line = raw_line.strip()
                    if line:
                        last_lines.append(line[-300:])
                        if len(last_lines) > 25:
                            last_lines.pop(0)
        finally:
            selector.close()
            proc.stdout.close()
        returncode = proc.wait()
        with self._lock:
            self._processes.pop(job.id, None)
        if job.id in self._cancel_requested or self._shutdown.is_set():
            if job.state != "cancelled":
                self._finish(job, "cancelled", percent, "cancelled", "Cancelado")
            return
        if returncode != 0:
            detail = " | ".join(last_lines[-5:]) or f"código {returncode}"
            self._finish(job, "error", percent, "error", "7z falló", detail[-2000:])
            return
        self.library.invalidate(dest_rel)
        message = f"Extraído en {dest_rel or '/'}"
        if job.delete_archive:
            try:
                archive.unlink()
                message += " · archivo eliminado"
            except OSError as exc:
                message += f" · no se pudo borrar el archivo: {exc}"
        self._finish(job, "done", 100, "done", message)

    def _inspect_archive_allow_existing(self, archive: Path) -> tuple[list[dict[str, Any]], int, str, Path]:
        # Re-run the listing but ignore the existing-target guard.
        try:
            result = subprocess.run(
                [self.seven_zip, "l", "-slt", str(archive)],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except FileNotFoundError as exc:
            raise JobError("No está instalado 7z. Instalá p7zip-full.") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            raise JobError(f"7z no pudo leer el archivo: {detail}")
        raw_entries = parse_7z_listing(result.stdout)
        entries: list[dict[str, Any]] = []
        total_size = 0
        for index, entry in enumerate(raw_entries):
            raw_path = str(entry.get("path", ""))
            if index == 0 and (
                entry.get("type") or Path(raw_path).name == archive.name
            ):
                continue
            safe = _safe_archive_member(raw_path)
            size = max(0, int(entry.get("size") or 0))
            is_dir = bool(entry.get("is_dir"))
            entries.append({"path": safe, "size": size, "is_dir": is_dir})
            if not is_dir:
                total_size += size
        if not entries:
            raise JobError("El archivo no contiene entradas")
        dest = archive.parent
        free = shutil.disk_usage(dest).free
        if total_size and free < total_size + 1024**3:
            raise JobError(
                f"Espacio insuficiente: se necesitan {human_size(total_size)} "
                f"y hay {human_size(free)} libres."
            )
        return entries, total_size, "", dest
