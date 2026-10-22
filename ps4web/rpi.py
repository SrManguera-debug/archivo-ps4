"""Client for the PS4 Remote Package Installer HTTP API."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

RPI_PORT = 12800
HEX_CHARS = "0123456789abcdefABCDEF"
HEX_RE = set(HEX_CHARS)


class RpiError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        body: str = "",
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body
        self.url = url


def _replace_hex_literals(text: str) -> str:
    """Convert unquoted 0xNN literals to decimal, leaving strings intact."""
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if (
            char == "0"
            and index + 2 < length
            and text[index + 1] in ("x", "X")
            and text[index + 2] in HEX_RE
        ):
            end = index + 2
            while end < length and text[end] in HEX_RE:
                end += 1
            # Only treat it as a number when it is not part of an identifier.
            previous = text[index - 1] if index > 0 else ""
            following = text[end] if end < length else ""
            if (not previous or previous not in HEX_CHARS + "_.$") and (
                not following or following not in HEX_CHARS + "_.$"
            ):
                out.append(str(int(text[index + 2 : end], 16)))
                index = end
                continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_lenient_json(text: str | bytes) -> dict[str, Any]:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = (text or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            value = json.loads(_replace_hex_literals(text))
        except json.JSONDecodeError as exc:
            preview = text[:500].replace("\n", " ")
            raise RpiError(f"Respuesta RPI no es JSON válido: {exc} ({preview})") from exc
    if not isinstance(value, dict):
        raise RpiError("La respuesta RPI debe ser un objeto JSON")
    return value


def _walk_values(value: Any, keys: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and not isinstance(item, (dict, list)):
                found.append(item)
            found.extend(_walk_values(item, keys))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_values(item, keys))
    return found


def _first(value: Any, keys: list[str]) -> Any:
    for key in keys:
        matches = _walk_values(value, {key})
        if matches:
            return matches[0]
    return None


def _find_any(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys:
                return item
        for item in value.values():
            found = _find_any(item, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_any(item, keys)
            if found is not None:
                return found
    return None


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    try:
        if text.lower().startswith(("0x", "-0x")):
            return int(text, 16)
        return int(float(text))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def extract_task_ids(response: dict[str, Any]) -> list[int]:
    values: list[int] = []
    for key in ("task_id", "taskId", "taskID"):
        for item in _walk_values(response, {key}):
            try:
                number = _as_int(item, -1)
            except Exception:
                continue
            if number >= 0 and number not in values:
                values.append(number)
    tasks = _find_any(response, {"tasks"})
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict):
                for key in ("task_id", "taskId", "id"):
                    if key in task:
                        number = _as_int(task[key], -1)
                        if number >= 0 and number not in values:
                            values.append(number)
                        break
    return values


def progress_snapshot(response: dict[str, Any]) -> dict[str, Any]:
    status = str(_first(response, ["status"]) or "").strip().lower()
    error_raw = _first(response, ["error"])
    error_code = _as_int(error_raw, 0)
    error_message = ""
    if status in ("fail", "failed", "error"):
        error_message = str(_first(response, ["message", "error_message"]) or "RPI reportó un error")
    elif error_code:
        error_message = f"RPI error {error_code}"

    length_total = _as_int(_first(response, ["length_total"]), 0)
    transferred_total = _as_int(_first(response, ["transferred_total"]), 0)
    length = _as_int(_first(response, ["length"]), 0)
    transferred = _as_int(_first(response, ["transferred"]), 0)
    if not length_total:
        length_total = length
    if not transferred_total:
        transferred_total = transferred
    preparing = _as_int(_first(response, ["preparing_percent"]), -1)
    local_copy = _as_int(_first(response, ["local_copy_percent"]), -1)
    num_index = _as_int(_first(response, ["num_index"]), 0)
    num_total = _as_int(_first(response, ["num_total"]), 0)
    rest_sec = _as_int(_first(response, ["rest_sec_total"]), 0) or _as_int(
        _first(response, ["rest_sec"]), 0
    )

    transfer_percent = 0.0
    if length_total > 0:
        transfer_percent = _clamp(transferred_total * 100.0 / length_total)
    elif transferred_total > 0:
        transfer_percent = 0.0

    phase = "downloading"
    percent = transfer_percent
    if error_message:
        phase = "error"
    elif local_copy >= 100:
        phase = "done"
        percent = 100.0
    elif local_copy >= 0 and (length_total <= 0 or transferred_total >= length_total):
        phase = "installing"
        percent = _clamp(local_copy)
    elif transferred_total > 0:
        phase = "downloading"
    elif preparing >= 0:
        phase = "preparing"
        percent = _clamp(preparing)
    elif length_total > 0:
        phase = "preparing"

    transfer_done = length_total > 0 and transferred_total >= length_total
    if transfer_done and local_copy < 100 and not error_message and phase != "installing":
        phase = "installing" if local_copy >= 0 else "finishing"
        if local_copy >= 0:
            percent = _clamp(local_copy)
    done = local_copy >= 100
    return {
        "status": status,
        "phase": phase,
        "percent": round(percent, 2),
        "transferred": transferred_total,
        "total": length_total,
        "transfer_percent": round(transfer_percent, 2),
        "transfer_done": transfer_done,
        "local_copy_percent": local_copy,
        "preparing_percent": preparing,
        "num_index": num_index,
        "num_total": num_total,
        "eta": rest_sec if rest_sec > 0 else None,
        "error": error_message or None,
        "error_code": error_code,
        "done": done,
        "raw": response,
    }


class RpiClient:
    def __init__(self, ip: str, port: int = RPI_PORT, timeout: float = 15.0) -> None:
        self.ip = ip
        self.port = int(port)
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    def _post(self, path: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        url = self.base_url + path
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "archivo-ps4/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            raw = exc.read(1024 * 1024).decode("utf-8", "replace")
            try:
                parsed = parse_lenient_json(raw)
                message = parsed.get("message") or parsed.get("error") or raw
            except RpiError:
                message = raw
            raise RpiError(f"RPI HTTP {exc.code}: {message}", status=exc.code, body=raw, url=url) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise RpiError(
                f"No se pudo conectar a {self.ip}:{self.port}: {exc}",
                url=url,
            ) from exc
        text = raw.decode("utf-8", "replace")
        return parse_lenient_json(text)

    def install(self, urls: list[str]) -> dict[str, Any]:
        clean = [str(url).strip() for url in urls if str(url).strip()]
        if not clean:
            raise RpiError("No hay URLs de PKG para instalar")
        response = self._post("/api/install", {"type": "direct", "packages": clean})
        if str(response.get("status", "")).lower() == "fail":
            raise RpiError(
                f"RPI rechazó la instalación: {response.get('error') or response}",
                body=json.dumps(response, ensure_ascii=False),
            )
        return response

    def progress(self, task_id: int) -> dict[str, Any]:
        if int(task_id) < 0:
            raise RpiError("task_id inválido")
        return self._post("/api/get_task_progress", {"task_id": int(task_id)})

    def pause(self, task_id: int) -> dict[str, Any]:
        return self._post("/api/pause_task", {"task_id": int(task_id)})

    def resume(self, task_id: int) -> dict[str, Any]:
        return self._post("/api/resume_task", {"task_id": int(task_id)})

    def stop(self, task_id: int) -> dict[str, Any]:
        return self._post("/api/stop_task", {"task_id": int(task_id)})

    def is_exists(self, title_id: str) -> dict[str, Any]:
        return self._post("/api/is_exists", {"title_id": str(title_id)})

    def health(self) -> bool:
        try:
            with socket.create_connection((self.ip, self.port), timeout=1.0):
                return True
        except OSError:
            return False
