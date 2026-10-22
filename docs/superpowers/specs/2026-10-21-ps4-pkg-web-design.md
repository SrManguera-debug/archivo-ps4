# PS4 Package Library — Design

Date: 2026-10-21
Status: approved for implementation
Working name: **Archivo PS4** (`ps4web`)

## Intent

A small, self-hosted web app that turns a LAN file server into a PS4 installation
station. From a phone or laptop the user picks a console, browses the PKG library,
sees the real game title, icon and required firmware, then sends base / update /
DLC packages to the console through Remote Package Installer (RPI). Everything
travels over the LAN; no USB stick.

The app is for one person on a trusted home network. It should feel like a
well-kept game archive, not a generic admin dashboard.

## Goals

1. **Multi-console** — register several PS4s, see which is online, choose the
   target for each install. Per-console sequential queues.
2. **Real metadata** — parse `param.sfo` from each PKG; show title, title ID,
   version, category and minimum firmware. Extract `icon0.png` from the PKG and
   show it in the library.
3. **Browse and select** — navigate directories under the library root, search,
   select individual PKGs or a whole game folder. Sort base → patch → DLC before
   queueing.
4. **Queue with progress** — persistent install queue, live progress from RPI,
   pause, resume, cancel and retry. Extraction jobs share the same queue UI.
5. **RAR extraction** — extract scene RARs on the server with `7z`, show
   progress, disk-space guard, never overwrite silently.
6. **Console preparation** — detect RPI / FTP / Remote Play ports; upload the RPI
   installer PKG over FTP when needed; show clear instructions.
7. **Firmware awareness** — optional per-console firmware field; warn when a PKG
   requires a newer firmware. Never silently claim compatibility.
8. **Secure enough for a LAN** — one admin account, PBKDF2 password, HttpOnly
   session cookie, CSRF header for mutations, path traversal protection. Not
   designed to be exposed to the internet.

## Non-goals

- Internet exposure, multi-user accounts, OAuth.
- Torrent / pyLoad / qBittorrent integration.
- Editing or deleting game files (RAR delete is opt-in and off by default).
- PS5, Xbox, Switch, or any console other than PS4.
- Bypassing minimum firmware. The app reports requirements; GoldHen does not
  change them.
- Transcoding, streaming, remote play.

## Architecture

```
phone / laptop
     |
     | HTTP :8081  (app: UI + JSON API + auth)
     v
  ps4web  (Python 3, stdlib only)  ---- RPI API :12800 ---->  PS4 (GoldHen + RPI)
     |                                   FTP :2121 (optional RPI upload)
     | reads metadata / runs 7z
     v
  /mnt/juegos_ps4/descargas  <---- HTTP :80 --- PS4 downloads PKG directly
        (nginx, Range enabled)         (nginx serves the same directory)
```

- The app never proxies the multi-gigabyte PKG body. It only gives the PS4 an
  nginx URL. nginx is the package CDN and already supports `Range`.
- RPI calls are made server-side, avoiding browser CORS and mixed-content
  problems.
- One worker thread per console processes install jobs sequentially. Extraction
  jobs run in a separate single worker. Shared state is guarded by locks.
- State is JSON on disk: `consoles.json`, `jobs.json`. Writes are atomic
  (temp file + `os.replace`).

### Project layout

```
ps4web/
  __init__.py
  __main__.py       CLI: serve, init-config, hash-password
  config.py         config load/save, password hashing, paths
  auth.py           sessions, login, CSRF
  pkg.py            PKG header/entry/SFO parsing, icon extraction, firmware
  library.py        safe path handling, directory listing, sorting, caches
  consoles.py       console registry, port probe, discovery, FTP upload
  rpi.py            RPI HTTP client, lenient JSON parser
  jobs.py           persistent queue, workers, progress, extraction
  server.py         ThreadingHTTPServer, routes, static files
  static/
    index.html
    login.html
    app.js
    style.css
tests/
  fake_rpi.py
  test_pkg.py
  test_library.py
  test_rpi.py
  test_jobs.py
  test_api.py
deploy/
  ps4web.service
  install.sh
```

## Data model

### Console

```json
{
  "id": "8f2c...",
  "name": "PS4 salón",
  "ip": "192.168.1.50",
  "rpi_port": 12800,
  "ftp_port": 2121,
  "firmware": "9.00",
  "notes": "",
  "created_at": 1760000000
}
```

`firmware` is optional. Status (`rpi`, `ftp`, `remote_play`, `online`) is probed
on demand and cached briefly; it is not persisted.

### Job

```json
{
  "id": "b41d...",
  "type": "install",
  "console_id": "8f2c...",
  "console_name": "PS4 salón",
  "package_path": "Street_Fighter_6/EP0102-...pkg",
  "url": "http://192.168.1.20/Street_Fighter_6/EP0102-...pkg",
  "title": "Street Fighter 6",
  "title_id": "CUSA31498",
  "state": "running",
  "progress": 42.5,
  "phase": "downloading",
  "task_id": 12,
  "message": "42.5% · 13.2 GiB / 31.1 GiB",
  "error": null,
  "created_at": 1760000000,
  "updated_at": 1760000012
}
```

States: `queued`, `starting`, `running`, `paused`, `extracting`, `done`, `error`,
`cancelled`. Install progress phases: `preparing`, `downloading`, `installing`,
`finishing`.

### PKG info

Parsed from the PKG header and `param.sfo`:

- `title`, `title_id`, `content_id`, `version` (SFO `VERSION`), `app_ver`
  (`APP_VER`), `category`, `system_ver` (raw int), `firmware` (display string),
  `size`, `mtime`.

Firmware formatting uses BCD bytes: `0x10008000` → `10.00.80`,
`0x04700000` → `4.70`, `0x05050000` → `5.05`.

## RPI protocol

Port 12800 (configurable per console; discovery also checks 12801).

- `POST /api/install` body `{"type":"direct","packages":["http://..."]}` →
  `{"status":"success","task_id":N,"title":"..."}`; on failure
  `{"status":"fail","error_code":...}`.
- `POST /api/get_task_progress` body `{"task_id":N}` → success object with
  `length`, `transferred`, `length_total`, `transferred_total`,
  `num_index`, `num_total`, `rest_sec`, `rest_sec_total`,
  `preparing_percent`, `local_copy_percent`, `error`, `bits`.
  **The numeric fields are written as `0x...` without quotes, which is invalid
  JSON.** `parse_lenient_json` rewrites hex literals to decimal before
  `json.loads`.
- `POST /api/pause_task`, `/api/resume_task`, `/api/stop_task` body
  `{"task_id":N}` → `{"status":"success"}`.
- `POST /api/is_exists` body `{"title_id":"CUSA..."}`.

Progress heuristic:

1. `error` non-zero → error.
2. If `length_total > 0`: percent = `transferred_total / length_total * 100`.
3. If `local_copy_percent >= 0`: phase `installing`, use it for the install
   portion; overall bar stays at 100 once transfer is complete.
4. `local_copy_percent >= 100` or (`transferred_total >= length_total > 0` and
   `num_index >= num_total > 0`) → done after a short confirmation window.
5. Otherwise keep polling every 2 s; surface `rest_sec` as ETA when available.

The app tolerates unknown extra fields and never crashes on a malformed progress
body; it stores the raw body in the job message for debugging.

## HTTP API

All JSON unless noted. Mutations require `X-CSRF-Token` matching the session.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | SPA (redirect to `/login` if no session) |
| GET | `/login` | login page |
| POST | `/api/login` | `{username,password}` → sets cookie |
| POST | `/api/logout` | clears session |
| GET | `/api/me` | `{user, csrf, config}` |
| GET | `/api/consoles` | consoles (no probing) |
| POST | `/api/consoles` | add |
| PUT | `/api/consoles/<id>` | edit |
| DELETE | `/api/consoles/<id>` | remove |
| GET | `/api/consoles/<id>/status` | port probe |
| GET | `/api/discover` | subnet scan candidates |
| POST | `/api/consoles/<id>/prepare-rpi` | FTP upload installer |
| GET | `/api/files?path=` | directory listing + breadcrumb + disk |
| GET | `/api/pkg?path=` | single PKG metadata |
| GET | `/api/icon?path=` | PNG icon (cached, ETag) |
| GET | `/api/jobs?console_id=` | job list |
| POST | `/api/jobs` | `{console_id, files:[...], include_dlc?}` |
| POST | `/api/extract` | `{path, delete_archive?}` |
| POST | `/api/jobs/<id>/pause` | pause |
| POST | `/api/jobs/<id>/resume` | resume |
| POST | `/api/jobs/<id>/cancel` | cancel |
| POST | `/api/jobs/<id>/retry` | requeue failed/cancelled |
| GET | `/api/stats` | disk + counts |

Install batch behavior: `files` may contain PKG paths and directory paths. Direct
PKGs are enqueued; directories are scanned recursively. If `include_dlc` is
false, packages whose category/filename indicate DLC are skipped. The batch is
sorted by title ID, then base (`gd`), patch (`gp`), DLC. Duplicate queued/running
packages for the same console are skipped.

## UI

### Visual direction

**A game archive meets a service manual.** Paper-toned background, ink text,
PlayStation blue as the structural accent, a single warm signal red for warnings.
Game titles in an editorial serif; dense metadata in a restrained monospace;
body in the system sans. Square-ish corners, hairline rules, no gradient washes,
no neon gamer tropes.

- Palette: `--paper #F1EEE5`, `--ink #17191C`, `--blue #0070D1`,
  `--red #D6452C`, `--green #2F7D5C`, `--muted #6E695F`, `--line #D8D3C6`.
- Type: display `"Iowan Old Style", "Palatino Linotype", Georgia, serif`;
  body `"Inter", system-ui, sans-serif`; data `"SFMono-Regular", Consolas,
  monospace`, used only for sizes / CUSA / versions.
- Layout: 300 px console rail on the left with a large status LED and queue
  counter; main column with a compact hero, breadcrumb, toolbar, dense library
  rows. Queue is a bottom sheet on mobile, right-side drawer on wide screens.
- Memorable element: the console rail reads like a physical rack unit — name,
  IP, LEDs for RPI/FTP/Remote Play, firmware, and a queue depth number.
- Library rows use 56 px icons, a blue left selection bar, and a firmware badge
  that turns red on incompatibility. No card grid: a list is faster to scan and
  more archive-like.
- Empty states explain the next action (e.g. “Esta carpeta no tiene PKGs.
  Extraé un RAR desde la fila del archivo.”).

### Screens

1. **Login** — wordmark, single password field, no username clutter (username
   fixed to `admin`, still sent). Error inline.
2. **Library** — default view. Console rail, hero with disk stats, breadcrumb,
   search, select all, include-DLC switch, install button, file rows, queue
   drawer.
3. **Console modal** — add/edit name, IP, RPI port, FTP port, firmware, notes;
   “Buscar en la red” fills discovered candidates.
4. **PKG detail drawer** — large icon, title, CUSA, version, firmware, path,
   install button.
5. **Queue** — all jobs or active console, progress bar, phase, ETA, pause /
   resume / cancel / retry, extraction jobs with cancel only.

### Interaction rules

- Active console is persisted in `localStorage`; if none, the first console is
  selected.
- “Instalar” requires an active console. If the console is offline, show a
  confirm warning but allow queueing (the worker will retry connection).
- Firmware mismatch shows a warning in the confirm dialog; the user can still
  proceed (the console will reject the package itself).
- Poll `/api/jobs` every 2 s while any job is active, every 10 s otherwise.
  Poll console status every 15 s.
- Toasts for success/error; no blocking `alert()` except destructive confirms.
- All POST/PUT/DELETE requests include the CSRF token.

## Security

- Password stored as PBKDF2-HMAC-SHA256 (200k iterations, random salt) in
  `/etc/ps4web/config.json` (mode 600). Generated on install and printed once.
- Session tokens are 32 random bytes, held in memory, cookie `HttpOnly;
  SameSite=Strict; Path=/`. Restarting the app logs everyone out.
- CSRF token per session, required for mutations.
- Path resolution: `realpath` must stay under the library root; reject symlinks
  that escape. Only `.pkg` / `.rar` are actionable.
- No shell: `subprocess` with argument lists. 7z archive paths are validated
  before extraction.
- URLs given to the PS4 are built only from `public_base_url` + validated
  relative paths; never accept arbitrary URLs from the client.
- Basic request body size limit (1 MiB) and JSON validation.
- App binds `0.0.0.0:8081` on the LAN; documentation warns not to port-forward.

## Persistence and failure behavior

- `consoles.json` and `jobs.json` are written atomically after mutations.
- On startup, jobs left in `starting`/`running`/`extracting` become `error`
  with “interrumpido por reinicio”; the RPI task may still be running on the
  PS4, so the user can retry or check the console.
- Job history is capped at 200 entries (oldest terminal jobs pruned).
- If a console goes offline during an install, the RPI call fails, the job goes
  to `error`, and retry re-enqueues it.
- Disk-space guard for extraction: require uncompressed size + 1 GiB free.
- Extraction destination is the archive's parent. If the target already exists,
  the job asks for confirmation via the API (`overwrite` flag); default is to
  fail rather than merge.

## Testing

- **Unit**: SFO/PKG parser against a synthetic PKG fixture and the real RPI PKG;
  firmware formatting; safe paths; sorting; lenient JSON; progress math.
- **Integration**: fake RPI HTTP server records install/pause/resume bodies and
  serves hex progress; job manager tests install → progress → pause/resume →
  done, extraction command construction, and failure paths.
- **HTTP**: start the real server on an ephemeral port with a temp library and
  fake RPI; login, CSRF rejection, traversal rejection, listing, icon, enqueue,
  pause.
- **Browser**: Playwright (Node) against the dev server: login, add console,
  browse, open PKG detail, enqueue, see progress, pause/resume, mobile viewport
  screenshot. Visual review of screenshots against this spec.
- **On-server smoke**: deploy, login, list the real library, fetch real icons
  and SFO, ensure nginx URL is reachable, run an extraction on a small archive.
- **Real console**: when the PS4 is online with GoldHen + RPI, install a small
  package, verify progress/pause/resume, then a full game.

## Deployment

- Target: Debian 12 LXC `192.168.1.20`, app on `:8081`, nginx on `:80` serving
  `/mnt/juegos_ps4/descargas`.
- `/opt/ps4web` (code), `/etc/ps4web/config.json` (secret), `/var/lib/ps4web`
  (state, icon cache).
- systemd unit `ps4web.service`, `Restart=on-failure`, `WorkingDirectory=/opt`,
  `Environment=PS4WEB_CONFIG=/etc/ps4web/config.json`.
- `install.sh` copies code, creates a config with a random password if absent,
  installs the unit, starts it, and prints the URL/credentials.
- nginx must keep `Range` support (already verified with `206 Partial Content`)
  and must not require auth for the PKG paths, because the PS4 has no session.
  Recommendation: keep nginx bound to the LAN / firewall it, and do not expose
  port 80 to the internet.

## Open questions / risks

- **PS4 not currently reachable.** Real install testing is blocked until the
  console is online and its IP/firmware are known. Everything up to the RPI call
  is testable with the fake server.
- **RPI progress completion signal is undocumented.** The heuristics above may
  need tuning against the real console; the raw progress is shown in the UI for
  diagnosis.
- **Standard RPI vs CPM RPI.** Some forks use port 12801 and accept spaces in
  URLs. The app makes the port configurable and percent-encodes URLs. If the
  standard RPI rejects encoded paths, rename packages to ASCII-safe names or use
  a CPM RPI build.
- **Firmware compatibility.** SF6 requires 10.00; MHW Iceborne backport is for
  5.05–6.72. The app warns but cannot make an incompatible package install.
- **RAR extraction needs `7z`.** Install `p7zip-full` on the server if absent;
  the app reports a clear error when the binary is missing.
