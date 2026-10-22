(() => {
  "use strict";

  const ACTIVE_STATES = new Set(["queued", "starting", "running", "paused", "extracting"]);
  const TERMINAL_STATES = new Set(["done", "error", "cancelled"]);
  const KIND_LABELS = { base: "Base", patch: "Update", dlc: "DLC", invalid: "Ilegible", other: "Otro" };
  const STATE_LABELS = {
    queued: "En cola",
    starting: "Iniciando",
    running: "En curso",
    paused: "Pausado",
    extracting: "Extrayendo",
    done: "Listo",
    error: "Error",
    cancelled: "Cancelado",
  };
  const FOLDER_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 6.5h6l2 2h9v9.5a1.5 1.5 0 0 1-1.5 1.5h-14A1.5 1.5 0 0 1 3.5 18z"/></svg>';
  const ARCHIVE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4.5h14v15H5z"/><path d="M9.5 4.5v4h5v-4M9.5 12h5M9.5 15.5h5"/></svg>';
  const EDIT_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 20 4.2-1 10.6-10.6a1.8 1.8 0 0 0 0-2.6l-.6-.6a1.8 1.8 0 0 0-2.6 0L5 15.8z"/><path d="m13.5 6.5 4 4"/></svg>';

  const dom = {
    app: document.getElementById("app"),
    boot: document.getElementById("boot"),
    fatal: document.getElementById("fatal"),
    fatalMessage: document.getElementById("fatal-message"),
    consoleList: document.getElementById("console-list"),
    btnAddConsole: document.getElementById("btn-add-console"),
    btnDiscover: document.getElementById("btn-discover"),
    btnMenu: document.getElementById("btn-menu"),
    railBackdrop: document.getElementById("rail-backdrop"),
    btnLogout: document.getElementById("btn-logout"),
    metaServer: document.getElementById("meta-server"),
    metaLibrary: document.getElementById("meta-library"),
    metaDisk: document.getElementById("meta-disk"),
    metaDiskBar: document.getElementById("meta-disk-bar"),
    diskAlert: document.getElementById("disk-alert"),
    diskAlertTitle: document.getElementById("disk-alert-title"),
    diskAlertText: document.getElementById("disk-alert-text"),
    diskAlertAction: document.getElementById("disk-alert-action"),
    metaVersion: document.getElementById("meta-version"),
    consoleEyebrow: document.getElementById("console-eyebrow"),
    consoleTitle: document.getElementById("console-title"),
    btnConsoleEdit: document.getElementById("btn-console-edit"),
    firmwareChip: document.getElementById("firmware-chip"),
    topStatusDot: document.getElementById("top-status-dot"),
    btnQueue: document.getElementById("btn-queue"),
    queueCount: document.getElementById("queue-count"),
    heroNote: document.getElementById("hero-note"),
    heroCount: document.getElementById("hero-count"),
    heroDirs: document.getElementById("hero-dirs"),
    heroSize: document.getElementById("hero-size"),
    breadcrumb: document.getElementById("breadcrumb"),
    search: document.getElementById("search"),
    btnRefresh: document.getElementById("btn-refresh"),
    selectAll: document.getElementById("select-all"),
    includeDlc: document.getElementById("include-dlc"),
    selectionCount: document.getElementById("selection-count"),
    btnInstall: document.getElementById("btn-install"),
    installConsole: document.getElementById("install-console"),
    fileList: document.getElementById("file-list"),
    emptyState: document.getElementById("empty-state"),
    emptyHint: document.getElementById("empty-hint"),
    fileLoading: document.getElementById("file-loading"),
    drawerBackdrop: document.getElementById("drawer-backdrop"),
    queueDrawer: document.getElementById("queue-drawer"),
    queueTitle: document.getElementById("queue-title"),
    queueFilterConsole: document.getElementById("queue-filter-console"),
    queueFilterAll: document.getElementById("queue-filter-all"),
    queueList: document.getElementById("queue-list"),
    queueEmpty: document.getElementById("queue-empty"),
    btnClearQueue: document.getElementById("btn-clear-queue"),
    detailDrawer: document.getElementById("detail-drawer"),
    detailKicker: document.getElementById("detail-kicker"),
    detailTitle: document.getElementById("detail-title"),
    detailBody: document.getElementById("detail-body"),
    consoleDialog: document.getElementById("console-dialog"),
    consoleForm: document.getElementById("console-form"),
    consoleDialogTitle: document.getElementById("console-dialog-title"),
    consoleName: document.getElementById("console-name"),
    consoleIp: document.getElementById("console-ip"),
    consoleRpiPort: document.getElementById("console-rpi-port"),
    consoleFtpPort: document.getElementById("console-ftp-port"),
    consoleFirmware: document.getElementById("console-firmware"),
    consoleNotes: document.getElementById("console-notes"),
    consoleFormError: document.getElementById("console-form-error"),
    btnSaveConsole: document.getElementById("btn-save-console"),
    btnDeleteConsole: document.getElementById("btn-delete-console"),
    btnDeleteSelected: document.getElementById("btn-delete-selected"),
    btnPrepareRpi: document.getElementById("btn-prepare-rpi"),
    btnDiscoverConsole: document.getElementById("btn-discover-console"),
    discoveryResults: document.getElementById("discovery-results"),
    confirmDialog: document.getElementById("confirm-dialog"),
    confirmKicker: document.getElementById("confirm-kicker"),
    confirmTitle: document.getElementById("confirm-title"),
    confirmMessage: document.getElementById("confirm-message"),
    confirmDetail: document.getElementById("confirm-detail"),
    confirmCancel: document.getElementById("confirm-cancel"),
    confirmAccept: document.getElementById("confirm-accept"),
    toasts: document.getElementById("toasts"),
  };

  const state = {
    me: null,
    consoles: [],
    activeConsoleId: null,
    statuses: {},
    files: null,
    path: "",
    search: "",
    selected: new Set(),
    includeDlc: false,
    jobs: [],
    queueFilter: "console",
    editingConsoleId: null,
    installing: false,
    deleting: false,
    clearingQueue: false,
    jobsFetching: false,
    confirmResolve: null,
    eventsBound: false,
    booted: false,
  };

  let jobsPollTimer = null;
  let statusTimer = null;

  class ApiError extends Error {
    constructor(message, status, data) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.data = data;
    }
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  function humanBytes(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n < 0) return "—";
    if (n < 1024) return `${n} B`;
    const units = ["KiB", "MiB", "GiB", "TiB"];
    let value = n / 1024;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2;
    return `${value.toFixed(digits)} ${units[index]}`;
  }

  function formatDate(epochSeconds) {
    if (!epochSeconds) return "—";
    try {
      return new Intl.DateTimeFormat("es-AR", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(Number(epochSeconds) * 1000));
    } catch {
      return "—";
    }
  }

  function versionParts(value) {
    return String(value || "0")
      .split(".")
      .map((part) => parseInt(part, 10))
      .filter((part) => Number.isFinite(part));
  }

  function compareVersions(a, b) {
    const left = versionParts(a);
    const right = versionParts(b);
    const length = Math.max(left.length, right.length);
    for (let i = 0; i < length; i += 1) {
      const l = left[i] || 0;
      const r = right[i] || 0;
      if (l !== r) return l < r ? -1 : 1;
    }
    return 0;
  }

  function firmwareNeedsNewer(required, current) {
    if (!required || !current) return null;
    const currentParts = versionParts(current);
    if (currentParts.length === 0) return null;
    return compareVersions(current, required) < 0;
  }

  function initials(value) {
    const words = String(value || "PKG")
      .replace(/[^\p{L}\p{N} ]+/gu, " ")
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    if (!words.length) return "PKG";
    if (words.length === 1) return words[0].slice(0, 3).toUpperCase();
    return `${words[0][0]}${words[1][0]}`.toUpperCase();
  }

  function kindLabel(entry) {
    if (entry.type === "dir") return "Carpeta";
    if (entry.type === "rar" || entry.type === "archive") return "Archivo";
    return KIND_LABELS[entry.kind] || "PKG";
  }

  function activeConsole() {
    return state.consoles.find((item) => item.id === state.activeConsoleId) || null;
  }

  function activeStatus() {
    const consoleItem = activeConsole();
    return consoleItem ? state.statuses[consoleItem.id] || null : null;
  }

  function findEntry(path) {
    if (!state.files || !Array.isArray(state.files.entries)) return null;
    return state.files.entries.find((entry) => entry.path === path) || null;
  }

  function isActiveJob(job) {
    return Boolean(job && ACTIVE_STATES.has(job.state));
  }

  function persistActiveConsole() {
    try {
      if (state.activeConsoleId) localStorage.setItem("ps4web.console", state.activeConsoleId);
      else localStorage.removeItem("ps4web.console");
    } catch {
      /* private mode */
    }
  }

  async function api(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = { Accept: "application/json" };
    let body;
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.body);
    }
    if (method !== "GET" && state.me && state.me.csrf) {
      headers["X-CSRF-Token"] = state.me.csrf;
    }
    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body,
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch {
      throw new ApiError("No se pudo contactar al servidor.", 0, null);
    }
    let data = null;
    const type = response.headers.get("content-type") || "";
    if (type.includes("application/json")) {
      data = await response.json().catch(() => null);
    } else {
      data = await response.text().catch(() => "");
    }
    if (response.status === 401) {
      if (!location.pathname.startsWith("/login")) {
        location.assign("/login");
      }
      throw new ApiError("La sesión venció.", 401, data);
    }
    if (!response.ok) {
      const message = data && typeof data === "object" && data.error
        ? data.error
        : typeof data === "string" && data
          ? data
          : `Error ${response.status}`;
      throw new ApiError(message, response.status, data);
    }
    return data;
  }

  function toast(message, type = "info", timeout = 5500) {
    if (!message) return;
    const node = document.createElement("div");
    node.className = `toast${type === "info" ? "" : ` ${type}`}`;
    const text = document.createElement("p");
    text.textContent = message;
    const close = document.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "Cerrar aviso");
    close.textContent = "×";
    close.addEventListener("click", () => node.remove());
    node.append(text, close);
    dom.toasts.append(node);
    const life = type === "error" ? Math.max(timeout, 7500) : timeout;
    window.setTimeout(() => node.remove(), life);
  }

  function setFatal(error) {
    if (!dom.fatal || !dom.fatalMessage) return;
    dom.boot.hidden = true;
    dom.app.hidden = true;
    dom.fatal.hidden = false;
    dom.fatalMessage.textContent = error && error.message
      ? error.message
      : "Revisá que el servidor esté funcionando y volvé a intentar.";
  }

  function closeRail() {
    dom.app.classList.remove("rail-open");
    dom.railBackdrop.hidden = true;
    dom.btnMenu.setAttribute("aria-expanded", "false");
  }

  function openRail() {
    dom.app.classList.add("rail-open");
    dom.railBackdrop.hidden = false;
    dom.btnMenu.setAttribute("aria-expanded", "true");
  }

  function serverLabel(url) {
    try {
      return new URL(url).host;
    } catch {
      return url || "—";
    }
  }

  function rootLabel(path) {
    if (!path) return "—";
    const parts = String(path).split("/").filter(Boolean);
    return parts[parts.length - 1] || path;
  }

  async function boot() {
    bindEvents();
    try {
      state.me = await api("/api/me");
    } catch (error) {
      if (error.status !== 401) setFatal(error);
      return;
    }

    try {
      state.includeDlc = localStorage.getItem("ps4web.includeDlc") === "1";
      state.queueFilter = localStorage.getItem("ps4web.queueAll") === "1" ? "all" : "console";
      const storedConsole = localStorage.getItem("ps4web.console");
      if (storedConsole) state.activeConsoleId = storedConsole;
    } catch {
      /* private mode */
    }
    dom.includeDlc.checked = state.includeDlc;

    const config = state.me.config || {};
    dom.metaServer.textContent = serverLabel(config.public_base_url);
    dom.metaServer.title = config.public_base_url || "";
    dom.metaLibrary.textContent = rootLabel(config.library_root);
    dom.metaLibrary.title = config.library_root || "";
    dom.metaVersion.textContent = config.app_version || "v1.0.0";

    dom.boot.hidden = true;
    dom.app.hidden = false;
    state.booted = true;

    const queryPath = new URLSearchParams(location.search).get("path") || "";
    try {
      await refreshConsoles();
    } catch (error) {
      if (error.status === 401) return;
      toast(error.message || "No se pudieron cargar las consolas.", "error");
      renderConsoles();
      renderTopbar();
    }
    try {
      await refreshJobs({ silent: true });
    } catch (error) {
      if (error.status === 401) return;
      toast(error.message || "No se pudo cargar la cola.", "error");
    }
    try {
      await loadFiles(queryPath, { push: false });
    } catch (error) {
      if (error.status === 401) return;
      toast(error.message || "No se pudo leer la biblioteca.", "error");
    }

    history.replaceState(
      { ps4path: state.path || "" },
      "",
      state.path ? `/?path=${encodeURIComponent(state.path)}` : "/",
    );
    startPolling();
  }

  function startPolling() {
    scheduleJobsPoll();
    if (statusTimer) window.clearInterval(statusTimer);
    statusTimer = window.setInterval(() => {
      if (document.hidden || !state.consoles.length) return;
      refreshStatuses().catch(() => {});
    }, 15000);
  }

  async function refreshConsoles() {
    const data = await api("/api/consoles");
    const consoles = Array.isArray(data.consoles) ? data.consoles : [];
    state.consoles = consoles;
    if (!consoles.some((item) => item.id === state.activeConsoleId)) {
      state.activeConsoleId = consoles[0] ? consoles[0].id : null;
      persistActiveConsole();
    }
    renderConsoles();
    renderTopbar();
    renderSelection();
    renderHero();
    if (state.files) renderFiles();
    if (consoles.length) {
      await refreshStatuses();
    }
  }

  async function refreshStatuses(force = false) {
    if (!state.consoles.length) return;
    const before = JSON.stringify(state.statuses);
    const results = await Promise.allSettled(
      state.consoles.map((consoleItem) =>
        api(`/api/consoles/${encodeURIComponent(consoleItem.id)}/status?force=${force ? "1" : "0"}`),
      ),
    );
    let authError = null;
    results.forEach((result, index) => {
      const consoleItem = state.consoles[index];
      if (!consoleItem) return;
      if (result.status === "fulfilled") {
        const payload = result.value || {};
        state.statuses[consoleItem.id] = payload.status || { online: false, rpi: false };
      } else if (result.reason && result.reason.status === 401) {
        authError = result.reason;
      } else {
        state.statuses[consoleItem.id] = {
          online: false,
          rpi: false,
          ftp: false,
          remote_play: false,
          error: true,
        };
      }
    });
    if (authError) throw authError;
    if (JSON.stringify(state.statuses) !== before) {
      renderConsoles();
      renderTopbar();
      renderHero();
      if (state.files) renderFiles();
    }
  }

  function statusOf(consoleItem) {
    return state.statuses[consoleItem.id] || consoleItem.status || null;
  }

  function renderConsoles() {
    if (!state.consoles.length) {
      dom.consoleList.innerHTML = `
        <div class="rail-empty">
          <strong>Sin consolas todavía.</strong>
          Agregá la IP de tu PS4 con GoldHen y RPI activo.
        </div>`;
      return;
    }
    dom.consoleList.innerHTML = state.consoles.map((consoleItem) => {
      const status = statusOf(consoleItem);
      const active = consoleItem.id === state.activeConsoleId;
      const dotClass = status ? (status.online ? "online" : "offline") : "unknown";
      const jobs = state.jobs.filter((job) => job.console_id === consoleItem.id && isActiveJob(job)).length;
      const rpiOn = Boolean(status && status.rpi);
      const ftpOn = Boolean(status && status.ftp);
      const remoteOn = Boolean(status && status.remote_play);
      const rpiPort = (status && status.rpi_port) || consoleItem.rpi_port;
      const ledClass = (on) => (status ? (on ? "on" : "off") : "");
      return `
        <div class="console-card${active ? " active" : ""}" data-console-id="${esc(consoleItem.id)}" role="button" tabindex="0" aria-pressed="${active}" title="${esc(consoleItem.name)}">
          <div class="console-top">
            <span class="status-dot ${dotClass}" aria-hidden="true"></span>
            <span class="console-name">${esc(consoleItem.name)}</span>
            ${jobs ? `<span class="console-count" title="${jobs} trabajo(s) activo(s)">${jobs}</span>` : ""}
          </div>
          <div class="console-sub">
            <span>${esc(consoleItem.ip)}:${esc(rpiPort)}</span>
            <span class="sep">/</span>
            <span>FW ${esc(consoleItem.firmware || "—")}</span>
          </div>
          <div class="console-leds" aria-label="Servicios detectados">
            <span class="led ${ledClass(rpiOn)}"><i></i>RPI</span>
            <span class="led ${ledClass(ftpOn)}"><i></i>FTP</span>
            <span class="led ${ledClass(remoteOn)}"><i></i>RP</span>
          </div>
          <button class="console-edit" type="button" data-edit-console="${esc(consoleItem.id)}" aria-label="Editar ${esc(consoleItem.name)}">
            ${EDIT_ICON}
          </button>
        </div>`;
    }).join("");
  }

  function renderTopbar() {
    const consoleItem = activeConsole();
    const status = activeStatus();
    dom.topStatusDot.className = "status-dot";
    if (!consoleItem) {
      dom.consoleEyebrow.textContent = "Sin consolas";
      dom.consoleTitle.textContent = "Agregá tu PS4";
      dom.firmwareChip.textContent = "Agregar consola";
      dom.topStatusDot.classList.add("unknown");
      dom.btnConsoleEdit.title = "Agregar consola";
    } else {
      dom.consoleEyebrow.textContent = "Consola activa";
      dom.consoleTitle.textContent = consoleItem.name;
      dom.firmwareChip.textContent = `FW ${consoleItem.firmware || "—"}`;
      dom.btnConsoleEdit.title = `Editar ${consoleItem.name}`;
      if (!status) dom.topStatusDot.classList.add("unknown");
      else dom.topStatusDot.classList.add(status.online ? "online" : "offline");
    }
    const activeJobs = state.jobs.filter(isActiveJob).length;
    dom.queueCount.textContent = String(activeJobs);
    dom.queueCount.classList.toggle("zero", activeJobs === 0);
    dom.installConsole.textContent = consoleItem ? consoleItem.name : "—";
  }

  function renderHero() {
    const entries = state.files && Array.isArray(state.files.entries) ? state.files.entries : [];
    const packages = entries.filter((entry) => entry.type === "pkg");
    const dirs = entries.filter((entry) => entry.type === "dir");
    const archives = entries.filter((entry) => entry.type === "rar" || entry.type === "archive");
    const totalSize = packages.reduce((sum, entry) => sum + (Number(entry.size) || 0), 0);
    dom.heroCount.textContent = String(packages.length);
    dom.heroDirs.textContent = String(dirs.length);
    dom.heroSize.textContent = totalSize ? humanBytes(totalSize) : "—";

    const consoleItem = activeConsole();
    const status = activeStatus();
    if (!consoleItem) {
      dom.heroNote.textContent = "Todavía no hay consolas. Agregá una desde la barra lateral para empezar.";
    } else if (status && !status.online) {
      dom.heroNote.textContent = `${consoleItem.name} no responde. Podés preparar la cola y encenderla cuando quieras.`;
    } else if (!entries.length) {
      dom.heroNote.textContent = "Esta carpeta está vacía. Elegí otra carpeta o extraé un archivo comprimido.";
    } else if (!packages.length && archives.length) {
      dom.heroNote.textContent = "Hay archivos comprimidos. Extraelos para que aparezcan los PKGs instalables.";
    } else {
      dom.heroNote.textContent = `Seleccioná PKGs y envialos a ${consoleItem.name} por la red local.`;
    }

    if (state.files && state.files.disk) {
      updateDiskUi(state.files.disk);
    } else {
      updateDiskUi(null);
    }
  }

  function updateDiskUi(disk) {
    const total = Number(disk && disk.total) || 0;
    const used = Number(disk && disk.used) || 0;
    const free = Number(disk && disk.free) || 0;
    const percent = total ? Math.min(100, Math.max(0, (used / total) * 100)) : 0;
    const freeThreshold = 10 * 1024 ** 3;
    const criticalFreeThreshold = 5 * 1024 ** 3;
    const low = total > 0 && (free < freeThreshold || percent >= 90);
    const critical = total > 0 && (free < criticalFreeThreshold || percent >= 95);

    dom.metaDisk.textContent = disk && disk.free_human ? `${disk.free_human} libres` : "—";
    dom.metaDisk.classList.toggle("low", low);
    dom.metaDisk.title = disk && disk.used_human && disk.total_human
      ? `${disk.used_human} usados de ${disk.total_human}${low ? ` · quedan ${disk.free_human}` : ""}`
      : "";
    dom.metaDiskBar.style.width = `${percent.toFixed(1)}%`;
    dom.metaDiskBar.classList.toggle("high", percent >= 85 && !critical);
    dom.metaDiskBar.classList.toggle("critical", critical);

    dom.diskAlert.hidden = !low;
    dom.diskAlert.classList.toggle("critical", critical);
    if (low) {
      dom.diskAlertTitle.textContent = critical ? "Disco casi lleno" : "Queda poco espacio";
      dom.diskAlertText.textContent = `Quedan ${disk.free_human} libres de ${disk.total_human}. La biblioteca está al ${percent.toFixed(0)}% de uso. Eliminá archivos o carpetas viejas para liberar espacio.`;
    }
  }

  function renderBreadcrumb() {
    const crumbs = state.files && Array.isArray(state.files.breadcrumb)
      ? state.files.breadcrumb
      : [{ name: "Biblioteca", path: "" }];
    dom.breadcrumb.innerHTML = crumbs.map((crumb, index) => {
      const current = index === crumbs.length - 1;
      const button = `<button type="button" data-crumb="${esc(crumb.path)}"${current ? ' aria-current="page"' : ""}>${esc(crumb.name)}</button>`;
      return index ? `<span class="crumb-sep" aria-hidden="true">/</span>${button}` : button;
    }).join("");
  }

  async function loadFiles(path, options = {}) {
    const { push = true, keepSearch = false, keepSelection = false } = options;
    const target = String(path || "");
    if (!keepSearch) {
      state.search = "";
      dom.search.value = "";
    }
    if (!keepSelection) state.selected.clear();
    dom.fileLoading.hidden = false;
    dom.emptyState.hidden = true;
    dom.fileList.innerHTML = "";
    dom.btnRefresh.disabled = true;
    try {
      const data = await api(`/api/files?path=${encodeURIComponent(target)}`);
      state.files = data;
      state.path = data.path || "";
      const allowed = new Set(
        (data.entries || []).filter(canSelectEntry).map((entry) => entry.path),
      );
      state.selected = new Set([...state.selected].filter((item) => allowed.has(item)));
      renderBreadcrumb();
      renderFiles();
      renderHero();
      renderSelection();
      if (push) {
        history.pushState(
          { ps4path: state.path },
          "",
          state.path ? `/?path=${encodeURIComponent(state.path)}` : "/",
        );
      }
      return true;
    } catch (error) {
      if (error.status === 401) return false;
      toast(error.message || "No se pudo abrir la carpeta.", "error");
      if (!state.files) {
        state.files = {
          path: "",
          breadcrumb: [{ name: "Biblioteca", path: "" }],
          entries: [],
          disk: null,
        };
        renderBreadcrumb();
        renderFiles();
      }
      return false;
    } finally {
      dom.fileLoading.hidden = true;
      dom.btnRefresh.disabled = false;
    }
  }

  function canSelectEntry(entry) {
    return Boolean(entry && entry.path);
  }

  function canInstallEntry(entry) {
    if (!entry) return false;
    if (entry.type === "dir") return true;
    return entry.type === "pkg" && entry.kind !== "invalid";
  }

  function getVisibleEntries() {
    if (!state.files || !Array.isArray(state.files.entries)) return [];
    const query = state.search.trim().toLocaleLowerCase("es");
    if (!query) return state.files.entries;
    return state.files.entries.filter((entry) => {
      const haystack = [
        entry.name,
        entry.title,
        entry.title_id,
        entry.content_id,
        entry.filename,
      ].filter(Boolean).join(" ").toLocaleLowerCase("es");
      return haystack.includes(query);
    });
  }

  function iconMarkup(entry, variant = "small") {
    if (entry.type === "dir") {
      return `<div class="file-icon folder" aria-hidden="true">${FOLDER_ICON}</div>`;
    }
    if (entry.type === "rar" || entry.type === "archive") {
      return `<div class="file-icon archive" aria-hidden="true">${ARCHIVE_ICON}</div>`;
    }
    const fallback = esc(initials(entry.title || entry.name));
    const image = entry.icon_url && entry.icon_available !== false
      ? `<img src="${esc(entry.icon_url)}" alt="" loading="lazy" decoding="async">`
      : "";
    if (variant === "large") {
      return `<div class="detail-icon"><span class="fallback">${fallback}</span>${image}</div>`;
    }
    return `<div class="file-icon"><span class="fallback">${fallback}</span>${image}</div>`;
  }

  function firmwareBadge(entry) {
    if (entry.type !== "pkg") return '<span aria-hidden="true"></span>';
    if (!entry.firmware) {
      return '<span class="fw-badge unknown" title="El paquete no declara firmware mínimo">FW ?</span>';
    }
    const consoleItem = activeConsole();
    const mismatch = consoleItem
      ? firmwareNeedsNewer(entry.firmware, consoleItem.firmware)
      : null;
    let css = "unknown";
    let title = `Requiere firmware ${entry.firmware} o superior`;
    if (mismatch === true) {
      css = "bad";
      title = `Requiere ${entry.firmware}; la consola declara ${consoleItem.firmware}`;
    } else if (mismatch === false) {
      css = "ok";
      title = `Compatible con el firmware declarado (${consoleItem.firmware})`;
    }
    return `<span class="fw-badge ${css}" title="${esc(title)}">FW ${esc(entry.firmware)}</span>`;
  }

  function rowSubline(entry) {
    const parts = [];
    if (entry.type === "dir") {
      const count = Number(entry.pkg_count) || 0;
      parts.push(`<span class="badge kind-dir">Carpeta</span>`);
      parts.push(`<span class="mono">${count} PKG${count === 1 ? "" : "s"}</span>`);
    } else if (entry.type === "pkg") {
      if (entry.title_id) parts.push(`<span class="mono">${esc(entry.title_id)}</span>`);
      const kind = entry.kind || "other";
      parts.push(`<span class="badge kind-${esc(kind)}">${esc(kindLabel(entry))}</span>`);
      if (entry.version) parts.push(`<span class="mono">v${esc(entry.version)}</span>`);
      if (entry.name && entry.title && entry.name !== entry.title) {
        parts.push(`<span class="filename">${esc(entry.name)}</span>`);
      }
      if (entry.metadata_error) {
        parts.push(`<span class="badge kind-invalid" title="${esc(entry.metadata_error)}">Metadatos</span>`);
      }
    } else if (entry.type === "rar" || entry.type === "archive") {
      parts.push(`<span class="badge kind-other">${esc((entry.archive_ext || "").replace(".", "").toUpperCase() || "Archivo")}</span>`);
      parts.push(`<span>Archivo comprimido</span>`);
    } else {
      parts.push(`<span class="badge kind-other">Archivo</span>`);
      parts.push(`<span>${esc(entry.name)}</span>`);
    }
    parts.push(`<span class="size-mobile">${esc(entry.size_human || humanBytes(entry.size))}</span>`);
    return parts.join('<span class="dot" aria-hidden="true">·</span>');
  }

  function deleteAction(entry) {
    return `<button class="text-button danger" type="button" data-delete-path="${esc(entry.path)}">Eliminar</button>`;
  }

  function rowActions(entry) {
    if (entry.type === "dir") {
      return `<button class="text-button" type="button" data-open-path="${esc(entry.path)}">Abrir</button>${deleteAction(entry)}`;
    }
    if (entry.type === "pkg") {
      if (entry.kind === "invalid") {
        return `<span class="busy-label" title="El PKG no se pudo leer">Ilegible</span>${deleteAction(entry)}`;
      }
      return `<button class="text-button install" type="button" data-install-path="${esc(entry.path)}">Instalar</button>${deleteAction(entry)}`;
    }
    if (entry.type === "rar" || entry.type === "archive") {
      const extracting = state.jobs.some(
        (job) => job.type === "extract" && job.package_path === entry.path && isActiveJob(job),
      );
      if (extracting) return '<span class="busy-label">Extrayendo…</span>';
      return `<button class="text-button extract" type="button" data-extract-path="${esc(entry.path)}">Extraer</button>${deleteAction(entry)}`;
    }
    return deleteAction(entry);
  }

  function rowHtml(entry) {
    const selected = state.selected.has(entry.path);
    const selectable = canSelectEntry(entry);
    const title = entry.title || entry.name;
    return `
      <div class="file-row${entry.type === "dir" ? " dir" : ""}${selected ? " selected" : ""}" data-path="${esc(entry.path)}" data-type="${esc(entry.type)}" role="listitem">
        <label class="row-check">
          <input type="checkbox" data-select-path="${esc(entry.path)}" ${selected ? "checked" : ""} ${selectable ? "" : "disabled"} aria-label="Seleccionar ${esc(title)}">
        </label>
        ${iconMarkup(entry)}
        <div class="file-main">
          <button class="file-title" type="button" data-open-path="${esc(entry.path)}" title="${esc(entry.metadata_error || title)}">${esc(title)}</button>
          <div class="file-sub">${rowSubline(entry)}</div>
        </div>
        <div class="file-fw">${firmwareBadge(entry)}</div>
        <div class="file-size">${entry.type === "dir" ? "" : esc(entry.size_human || humanBytes(entry.size))}</div>
        <div class="file-actions">${rowActions(entry)}</div>
      </div>`;
  }

  function renderFiles() {
    if (!state.files) return;
    const visible = getVisibleEntries();
    dom.fileList.innerHTML = visible.map(rowHtml).join("");
    const empty = visible.length === 0;
    dom.emptyState.hidden = !empty;
    if (empty) {
      const eyebrow = dom.emptyState.querySelector(".eyebrow");
      const heading = dom.emptyState.querySelector("h2");
      if (state.search.trim()) {
        if (eyebrow) eyebrow.textContent = "Sin coincidencias";
        if (heading) heading.textContent = "No encontramos ese título.";
        dom.emptyHint.textContent = `Probá con otro nombre, CUSA o archivo. La búsqueda es solo en esta carpeta.`;
      } else if (!state.files.entries || !state.files.entries.length) {
        if (eyebrow) eyebrow.textContent = "Carpeta vacía";
        if (heading) heading.textContent = "Acá no hay nada para instalar.";
        dom.emptyHint.textContent = "Elegí otra carpeta o extraé un archivo comprimido desde el explorador.";
      } else {
        if (eyebrow) eyebrow.textContent = "Nada instalable";
        if (heading) heading.textContent = "Solo hay archivos comprimidos.";
        dom.emptyHint.textContent = "Extraé un RAR o 7z con el botón de la fila para que aparezcan los PKGs.";
      }
    }
    attachIconFallbacks(dom.fileList);
    updateSelectionUI();
  }

  function attachIconFallbacks(container) {
    if (!container) return;
    container.querySelectorAll(".file-icon img, .detail-icon img").forEach((image) => {
      const fail = () => image.remove();
      image.addEventListener("error", fail, { once: true });
      if (image.complete && image.naturalWidth === 0) fail();
    });
  }

  function renderSelection() {
    updateSelectionUI();
  }

  function updateSelectionUI() {
    const visibleSelectable = getVisibleEntries().filter(canSelectEntry);
    const visibleSelected = visibleSelectable.filter((entry) => state.selected.has(entry.path));
    dom.selectAll.checked = visibleSelectable.length > 0 && visibleSelected.length === visibleSelectable.length;
    dom.selectAll.indeterminate = visibleSelected.length > 0 && visibleSelected.length < visibleSelectable.length;
    dom.selectAll.disabled = visibleSelectable.length === 0;
    const selectedEntries = [...state.selected].map(findEntry).filter(Boolean);
    const installableEntries = selectedEntries.filter(canInstallEntry);
    const count = selectedEntries.length;
    dom.selectionCount.textContent = count
      ? `${count} seleccionado${count === 1 ? "" : "s"}${installableEntries.length === count ? "" : ` · ${installableEntries.length} instalable${installableEntries.length === 1 ? "" : "s"}`}`
      : "Nada seleccionado";
    const consoleItem = activeConsole();
    dom.btnInstall.disabled = !consoleItem || installableEntries.length === 0 || state.installing || installableEntries.length !== count;
    dom.btnDeleteSelected.disabled = count === 0 || state.deleting;
    dom.btnDeleteSelected.title = count
      ? `Eliminar ${count} elemento(s) del almacenamiento`
      : "Seleccioná archivos o carpetas para eliminar";
    dom.installConsole.textContent = consoleItem ? consoleItem.name : "—";
    dom.btnInstall.title = !consoleItem
      ? "Agregá una consola para instalar"
      : installableEntries.length !== count
        ? "La selección incluye archivos que no se pueden instalar"
        : `Encolar ${count} elemento(s) para ${consoleItem.name}`;
    dom.fileList.querySelectorAll(".file-row").forEach((row) => {
      const path = row.dataset.path;
      const selected = state.selected.has(path);
      row.classList.toggle("selected", selected);
      const checkbox = row.querySelector("[data-select-path]");
      if (checkbox) checkbox.checked = selected;
    });
  }

  function navigate(path) {
    closeDrawers();
    closeRail();
    return loadFiles(path, { push: true });
  }

  function openEntry(entry) {
    if (!entry) return;
    if (entry.type === "dir") {
      navigate(entry.path);
      return;
    }
    if (entry.type === "pkg" || entry.type === "rar" || entry.type === "archive") {
      openDetail(entry);
    }
  }

  let lastDrawerFocus = null;

  function bodyLock(locked) {
    document.body.classList.toggle("is-locked", locked);
  }

  function openDrawer(kind) {
    closeDrawers();
    lastDrawerFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const drawer = kind === "queue" ? dom.queueDrawer : dom.detailDrawer;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    dom.drawerBackdrop.hidden = false;
    bodyLock(true);
    const close = drawer.querySelector("[data-close-drawer]");
    if (close) close.focus();
  }

  function closeDrawer(kind) {
    const drawer = kind === "queue" ? dom.queueDrawer : dom.detailDrawer;
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    const anyOpen = dom.queueDrawer.classList.contains("open") || dom.detailDrawer.classList.contains("open");
    if (!anyOpen) {
      dom.drawerBackdrop.hidden = true;
      bodyLock(false);
      if (lastDrawerFocus && document.contains(lastDrawerFocus)) {
        lastDrawerFocus.focus();
      }
      lastDrawerFocus = null;
    }
  }

  function closeDrawers() {
    dom.queueDrawer.classList.remove("open");
    dom.detailDrawer.classList.remove("open");
    dom.queueDrawer.setAttribute("aria-hidden", "true");
    dom.detailDrawer.setAttribute("aria-hidden", "true");
    dom.drawerBackdrop.hidden = true;
    bodyLock(false);
    if (lastDrawerFocus && document.contains(lastDrawerFocus)) {
      lastDrawerFocus.focus();
    }
    lastDrawerFocus = null;
  }

  function openQueueDrawer() {
    renderQueue();
    openDrawer("queue");
  }

  function detailKindLabel(entry) {
    if (entry.type === "rar" || entry.type === "archive") {
      return `Archivo ${String(entry.archive_ext || "").replace(".", "").toUpperCase() || "comprimido"}`;
    }
    if (entry.kind === "base") return "Paquete base";
    if (entry.kind === "patch") return "Actualización";
    if (entry.kind === "dlc") return "Contenido adicional";
    if (entry.kind === "invalid") return "PKG ilegible";
    return "Paquete PKG";
  }

  function detailGrid(rows) {
    return `<dl class="detail-grid">${rows.map(([label, value]) => `
      <div><dt>${esc(label)}</dt><dd title="${esc(value)}">${esc(value)}</dd></div>`).join("")}</dl>`;
  }

  function openDetail(entry) {
    if (!entry) return;
    const consoleItem = activeConsole();
    const title = entry.title || entry.name;
    dom.detailKicker.textContent = detailKindLabel(entry);
    dom.detailTitle.textContent = title;
    dom.detailTitle.title = title;

    let body = `
      <div class="detail-hero">
        ${iconMarkup(entry, "large")}
        <div class="detail-hero-copy">
          <p class="eyebrow">${esc(detailKindLabel(entry))}</p>
          <h3>${esc(title)}</h3>
          <p>${esc(entry.name || "")}<br>${esc(entry.size_human || humanBytes(entry.size))}</p>
        </div>
      </div>`;

    if (entry.type === "pkg") {
      const rows = [
        ["Título ID", entry.title_id || "—"],
        ["Contenido", entry.content_id || "—"],
        ["Versión", entry.version ? `${entry.version}${entry.app_ver ? ` (${entry.app_ver})` : ""}` : "—"],
        ["Categoría", entry.category || kindLabel(entry)],
        ["Firmware mínimo", entry.firmware || "No declarado"],
        ["Tamaño", entry.size_human || humanBytes(entry.size)],
        ["Modificado", formatDate(entry.mtime)],
      ];
      body += detailGrid(rows);

      const mismatch = consoleItem
        ? firmwareNeedsNewer(entry.firmware, consoleItem.firmware)
        : null;
      if (entry.metadata_error) {
        body += `<p class="detail-note bad">No se pudieron leer los metadatos: ${esc(entry.metadata_error)}</p>`;
      } else if (mismatch === true) {
        body += `<p class="detail-note bad">Este PKG requiere firmware ${esc(entry.firmware)} y ${esc(consoleItem.name)} declara ${esc(consoleItem.firmware)}. La instalación podría fallar.</p>`;
      } else if (mismatch === null && entry.firmware && (!consoleItem || !consoleItem.firmware)) {
        body += `<p class="detail-note">El PKG declara firmware ${esc(entry.firmware)}. Agregá el firmware de la consola para comparar.</p>`;
      }
      if (consoleItem && !activeStatus()) {
        body += `<p class="detail-note">La consola todavía no respondió al sondeo. El trabajo quedará en cola si no está disponible.</p>`;
      } else if (consoleItem && activeStatus() && !activeStatus().online) {
        body += `<p class="detail-note">La consola no responde en la red. Podés encolar la instalación igual.</p>`;
      }
    } else {
      const rows = [
        ["Tipo", detailKindLabel(entry)],
        ["Tamaño", entry.size_human || humanBytes(entry.size)],
        ["Modificado", formatDate(entry.mtime)],
      ];
      body += detailGrid(rows);
      body += `<p class="detail-note">La extracción se hace en el servidor, dentro de esta carpeta. Puede tardar varios minutos y necesita espacio libre suficiente.</p>`;
    }

    body += `<p class="detail-path">${esc(entry.path)}</p>`;
    body += '<div class="detail-actions">';
    if (entry.type === "pkg" && entry.kind !== "invalid") {
      body += `<button class="button primary" type="button" data-detail-install="${esc(entry.path)}" ${consoleItem ? "" : "disabled"}>Instalar en ${esc(consoleItem ? consoleItem.name : "—")}</button>`;
      body += `<button class="button secondary" type="button" data-detail-select="${esc(entry.path)}">${state.selected.has(entry.path) ? "Quitar de selección" : "Agregar a selección"}</button>`;
    }
    if (entry.type === "rar" || entry.type === "archive") {
      body += `<button class="button primary" type="button" data-detail-extract="${esc(entry.path)}">Extraer acá</button>`;
    }
    body += `<button class="button danger" type="button" data-detail-delete="${esc(entry.path)}">Eliminar del almacenamiento</button>`;
    body += "</div>";

    dom.detailBody.innerHTML = body;
    attachIconFallbacks(dom.detailBody);
    openDrawer("detail");
  }

  function renderQueue() {
    const consoleItem = activeConsole();
    const all = Array.isArray(state.jobs) ? state.jobs : [];
    const useAll = state.queueFilter === "all" || !consoleItem;
    const jobs = (useAll ? all : all.filter((job) => job.console_id === consoleItem.id))
      .slice()
      .sort((a, b) => {
        const activeDiff = Number(isActiveJob(b)) - Number(isActiveJob(a));
        if (activeDiff) return activeDiff;
        return (Number(b.created_at) || 0) - (Number(a.created_at) || 0);
      });

    dom.queueFilterConsole.classList.toggle("active", !useAll);
    dom.queueFilterAll.classList.toggle("active", useAll);
    dom.queueFilterConsole.setAttribute("aria-pressed", String(!useAll));
    dom.queueFilterAll.setAttribute("aria-pressed", String(useAll));
    dom.queueFilterConsole.disabled = !consoleItem;
    dom.queueFilterConsole.textContent = consoleItem ? "Esta consola" : "Sin consola";
    const activeCount = jobs.filter(isActiveJob).length;
    const terminalCount = jobs.filter((job) => TERMINAL_STATES.has(job.state)).length;
    dom.btnClearQueue.disabled = state.clearingQueue || terminalCount === 0;
    dom.btnClearQueue.title = terminalCount
      ? `Quitar ${terminalCount} trabajo${terminalCount === 1 ? "" : "s"} terminado${terminalCount === 1 ? "" : "s"}, fallido${terminalCount === 1 ? "" : "s"} o cancelado${terminalCount === 1 ? "" : "s"}. Los activos no se tocan.`
      : "No hay trabajos terminados para limpiar.";
    dom.queueTitle.textContent = activeCount ? `Cola de trabajos · ${activeCount}` : "Cola de trabajos";
    dom.queueEmpty.hidden = jobs.length > 0;
    if (!jobs.length) {
      dom.queueList.innerHTML = "";
      const heading = dom.queueEmpty.querySelector("h3");
      const copy = dom.queueEmpty.querySelector("p:last-child");
      if (useAll && !consoleItem) {
        if (heading) heading.textContent = "La cola está vacía.";
        if (copy) copy.textContent = "Agregá una consola y seleccioná un PKG para empezar.";
      } else if (!useAll) {
        if (heading) heading.textContent = "Nada para esta consola.";
        if (copy) copy.textContent = "Cambiá el filtro a Todas o encolá una instalación.";
      } else {
        if (heading) heading.textContent = "La cola está vacía.";
        if (copy) copy.textContent = "Seleccioná un PKG y mandate a instalar. Acá vas a ver el progreso.";
      }
      return;
    }

    dom.queueList.innerHTML = jobs.map((job) => {
      const pct = Math.min(100, Math.max(0, Number(job.progress) || 0));
      const shownPct = job.state === "queued" ? 0 : Math.round(pct);
      const stateClass = TERMINAL_STATES.has(job.state) || ACTIVE_STATES.has(job.state) ? job.state : "queued";
      const title = job.title || (job.package_path ? job.package_path.split("/").pop() : "Trabajo");
      const meta = [job.console_name, job.title_id, job.size_human].filter(Boolean).join(" · ");
      const message = job.state === "error" ? (job.error || job.message || "") : (job.message || "");
      const actions = [];
      if (job.can_pause) actions.push(`<button class="text-button" type="button" data-job-action="pause" data-job-id="${esc(job.id)}">Pausar</button>`);
      if (job.can_resume) actions.push(`<button class="text-button" type="button" data-job-action="resume" data-job-id="${esc(job.id)}">Reanudar</button>`);
      if (job.can_retry) actions.push(`<button class="text-button" type="button" data-job-action="retry" data-job-id="${esc(job.id)}">Reintentar</button>`);
      if (job.can_cancel) actions.push(`<button class="text-button" type="button" data-job-action="cancel" data-job-id="${esc(job.id)}">Cancelar</button>`);
      return `
        <article class="job-card ${esc(stateClass)}" data-job-id="${esc(job.id)}">
          <div class="job-top">
            <span class="job-type">${esc(job.type_label || (job.type === "extract" ? "Extracción" : "Instalación"))}</span>
            <span class="state-pill ${esc(stateClass)}">${esc(STATE_LABELS[job.state] || job.state)}</span>
          </div>
          <h3 class="job-title" title="${esc(title)}">${esc(title)}</h3>
          <p class="job-meta">${esc(meta || "Servidor")}</p>
          <div class="progress-row">
            <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${shownPct}"><span data-progress="${shownPct}"></span></div>
            <span class="progress-number">${shownPct}%</span>
          </div>
          ${message ? `<p class="${job.state === "error" ? "job-error" : "job-message"}">${esc(message)}</p>` : ""}
          ${actions.length ? `<div class="job-actions">${actions.join("")}</div>` : ""}
        </article>`;
    }).join("");

    dom.queueList.querySelectorAll("[data-progress]").forEach((bar) => {
      bar.style.width = `${bar.dataset.progress}%`;
    });
  }

  function askConfirm(options) {
    const {
      title = "¿Continuar?",
      kicker = "Confirmar",
      message = "",
      detail = "",
      confirmLabel = "Continuar",
      altLabel = "",
      danger = false,
      altDanger = true,
    } = options || {};
    return new Promise((resolve) => {
      if (state.confirmResolve) {
        const previous = state.confirmResolve;
        state.confirmResolve = null;
        previous(false);
      }
      if (dom.confirmDialog.open) dom.confirmDialog.close();
      state.confirmResolve = resolve;
      dom.confirmKicker.textContent = kicker;
      dom.confirmTitle.textContent = title;
      dom.confirmMessage.textContent = message;
      dom.confirmDetail.textContent = detail;
      dom.confirmDetail.hidden = !detail;
      dom.confirmAccept.textContent = confirmLabel;
      dom.confirmAccept.className = `button ${danger ? "danger" : "primary"}`;
      const alt = document.getElementById("confirm-alt");
      if (alt) {
        alt.hidden = !altLabel;
        alt.textContent = altLabel || "";
        alt.className = `button ${altDanger ? "danger" : "secondary"}`;
      }
      dom.confirmDialog.showModal();
    });
  }

  function settleConfirm(value) {
    const resolve = state.confirmResolve;
    state.confirmResolve = null;
    if (dom.confirmDialog.open) dom.confirmDialog.close();
    if (resolve) resolve(value);
  }

  function firmwareWarnings(entries) {
    const consoleItem = activeConsole();
    if (!consoleItem || !consoleItem.firmware) return [];
    const warnings = [];
    const seen = new Set();
    entries.forEach((entry) => {
      if (!entry || entry.type !== "pkg" || !entry.firmware) return;
      if (firmwareNeedsNewer(entry.firmware, consoleItem.firmware) !== true) return;
      const title = entry.title || entry.name;
      if (seen.has(title)) return;
      seen.add(title);
      warnings.push(`${title}: requiere FW ${entry.firmware}, la consola declara ${consoleItem.firmware}.`);
    });
    return warnings;
  }

  async function installPaths(paths, triggerButton = null) {
    const unique = [...new Set((paths || []).filter(Boolean))];
    if (!unique.length) return;
    const consoleItem = activeConsole();
    if (!consoleItem) {
      toast("Agregá una consola antes de instalar.", "warning");
      openConsoleDialog();
      return;
    }
    if (state.installing) return;

    const entries = unique.map(findEntry).filter(Boolean);
    const warnings = firmwareWarnings(entries);
    const status = activeStatus();
    if (status && !status.rpi) {
      warnings.unshift(`RPI no responde en ${consoleItem.ip}:${consoleItem.rpi_port}. El trabajo quedará en cola hasta que la consola esté disponible.`);
    }
    if (warnings.length) {
      const confirmed = await askConfirm({
        kicker: "Antes de enviar",
        title: "Hay advertencias",
        message: `Se van a encolar ${unique.length} elemento(s) para ${consoleItem.name}. Revisá esto antes de continuar.`,
        detail: warnings.slice(0, 6).join("\n") + (warnings.length > 6 ? `\n… y ${warnings.length - 6} más.` : ""),
        confirmLabel: "Encolar igual",
      });
      if (!confirmed) return;
    }

    state.installing = true;
    updateSelectionUI();
    if (triggerButton) triggerButton.disabled = true;
    try {
      const data = await api("/api/jobs", {
        method: "POST",
        body: {
          console_id: consoleItem.id,
          files: unique,
          include_dlc: state.includeDlc,
        },
      });
      const created = Array.isArray(data.jobs) ? data.jobs : [];
      const serverWarnings = Array.isArray(data.warnings) ? data.warnings : [];
      serverWarnings.slice(0, 3).forEach((warning) => toast(warning, "warning", 8000));
      if (serverWarnings.length > 3) {
        toast(`${serverWarnings.length - 3} advertencia(s) más en la cola.`, "warning");
      }
      toast(`${created.length || unique.length} paquete(s) en cola para ${consoleItem.name}.`, "success");
      state.selected.clear();
      updateSelectionUI();
      await refreshJobs({ silent: true });
      openQueueDrawer();
    } catch (error) {
      toast(error.message || "No se pudo encolar la instalación.", "error");
    } finally {
      state.installing = false;
      updateSelectionUI();
      if (triggerButton) triggerButton.disabled = false;
    }
  }

  async function extractArchive(path, triggerButton = null) {
    const entry = findEntry(path);
    if (!entry) return;
    const already = state.jobs.some(
      (job) => job.type === "extract" && job.package_path === entry.path && isActiveJob(job),
    );
    if (already) {
      toast("Ese archivo ya tiene una extracción en curso.", "warning");
      openQueueDrawer();
      return;
    }
    const choice = await askConfirm({
      kicker: "Extracción",
      title: "Extraer archivo",
      message: `Se va a descomprimir ${entry.name} en la carpeta actual del servidor.`,
      detail: "Puede tardar varios minutos. Por seguridad no se sobrescriben archivos existentes salvo que lo pidas explícitamente.",
      confirmLabel: "Extraer",
      altLabel: "Extraer y sobrescribir",
    });
    if (!choice) return;
    const overwrite = choice === "alt";
    if (triggerButton) triggerButton.disabled = true;
    try {
      await api("/api/extract", {
        method: "POST",
        body: { path: entry.path, overwrite, delete_archive: false },
      });
      toast(`Extracción en cola: ${entry.name}`, "success");
      await refreshJobs({ silent: true });
      openQueueDrawer();
    } catch (error) {
      toast(error.message || "No se pudo encolar la extracción.", "error");
    } finally {
      if (triggerButton) triggerButton.disabled = false;
    }
  }

  async function deletePaths(paths, triggerButton = null) {
    const unique = [...new Set((paths || []).filter(Boolean))];
    if (!unique.length || state.deleting) return;
    const entries = unique.map(findEntry).filter(Boolean);
    if (!entries.length) return;
    const folders = entries.filter((entry) => entry.type === "dir");
    const detail = entries.slice(0, 10).map((entry) => (
      `${entry.type === "dir" ? "Carpeta" : "Archivo"}: ${entry.name}`
    ));
    if (entries.length > 10) detail.push(`… y ${entries.length - 10} más.`);
    if (folders.length) detail.push("Las carpetas se eliminan junto con todo su contenido.");
    const confirmed = await askConfirm({
      kicker: "Eliminar del almacenamiento",
      title: entries.length === 1 ? "Eliminar elemento" : `Eliminar ${entries.length} elementos`,
      message: "Se van a borrar permanentemente del servidor. Esta acción no se puede deshacer.",
      detail: detail.join("\n"),
      confirmLabel: "Eliminar",
      danger: true,
    });
    if (!confirmed) return;

    state.deleting = true;
    updateSelectionUI();
    if (triggerButton) triggerButton.disabled = true;
    try {
      const data = await api("/api/files/delete", {
        method: "POST",
        body: { paths: unique },
      });
      const deleted = Array.isArray(data.deleted) ? data.deleted : [];
      const errors = Array.isArray(data.errors) ? data.errors : [];
      if (deleted.length) {
        deleted.forEach((path) => state.selected.delete(path));
        toast(`${deleted.length} elemento(s) eliminado(s) del almacenamiento.`, "success");
        if (dom.detailDrawer.classList.contains("open")) closeDrawer("detail");
        await loadFiles(state.path, { push: false, keepSearch: true, keepSelection: true });
      }
      if (errors.length) {
        toast(`${errors.length} elemento(s) no se pudieron eliminar: ${errors[0].error}`, "error", 9000);
      }
      if (!deleted.length && !errors.length) {
        toast("No se eliminó ningún elemento.", "warning");
      }
    } catch (error) {
      toast(error.message || "No se pudieron eliminar los elementos.", "error");
    } finally {
      state.deleting = false;
      updateSelectionUI();
      if (triggerButton) triggerButton.disabled = false;
    }
  }

  function notifyJobTransitions(previous, next) {
    const finished = [];
    next.forEach((job) => {
      const old = previous.get(job.id);
      if (!old || old === job.state) return;
      if (job.state === "done") {
        finished.push({
          type: "success",
          message: job.type === "extract"
            ? `Extracción completa: ${job.title}`
            : `Instalación completa: ${job.title}`,
        });
      } else if (job.state === "error") {
        finished.push({
          type: "error",
          message: `Falló ${job.type === "extract" ? "la extracción" : "la instalación"} de ${job.title}: ${job.error || "error desconocido"}`,
        });
      }
    });
    if (!finished.length) return;
    if (finished.length <= 2) {
      finished.forEach((item) => toast(item.message, item.type, item.type === "error" ? 9000 : 6500));
    } else {
      const errors = finished.filter((item) => item.type === "error").length;
      if (errors) toast(`${errors} trabajo(s) fallaron. Abrí la cola para ver el detalle.`, "error", 9000);
      const successes = finished.length - errors;
      if (successes) toast(`${successes} trabajo(s) terminaron correctamente.`, "success");
    }
  }

  async function refreshJobs(options = {}) {
    if (state.jobsFetching) return;
    state.jobsFetching = true;
    try {
      const data = await api("/api/jobs");
      const next = Array.isArray(data.jobs) ? data.jobs : [];
      const previous = new Map(state.jobs.map((job) => [job.id, job.state]));
      const extractDone = next.some(
        (job) => job.type === "extract" && job.state === "done" && previous.get(job.id) && previous.get(job.id) !== "done",
      );
      notifyJobTransitions(previous, next);
      state.jobs = next;
      if (data.disk && state.files) {
        state.files.disk = data.disk;
        updateDiskUi(data.disk);
      }
      renderQueue();
      renderConsoles();
      renderTopbar();
      if (state.files && (extractDone || next.some((job) => job.type === "extract" && isActiveJob(job)))) {
        renderFiles();
      }
      if (extractDone && state.files) {
        loadFiles(state.path, { push: false, keepSearch: true, keepSelection: true }).catch(() => {});
      }
      scheduleJobsPoll();
    } catch (error) {
      if (error.status === 401) throw error;
      if (!options.silent) toast(error.message || "No se pudo actualizar la cola.", "error");
    } finally {
      state.jobsFetching = false;
    }
  }

  function scheduleJobsPoll() {
    if (jobsPollTimer) window.clearTimeout(jobsPollTimer);
    const active = state.jobs.some(isActiveJob);
    const delay = active ? 2000 : 10000;
    jobsPollTimer = window.setTimeout(async () => {
      if (!document.hidden) {
        try {
          await refreshJobs({ silent: true });
        } catch {
          /* retry next tick */
        }
      }
      scheduleJobsPoll();
    }, delay);
  }

  async function jobAction(jobId, action, button) {
    const job = state.jobs.find((item) => item.id === jobId);
    if (!job) return;
    if (action === "cancel") {
      const confirmed = await askConfirm({
        kicker: "Cancelar",
        title: "Detener el trabajo",
        message: `Se va a cancelar ${job.type === "extract" ? "la extracción" : "la instalación"} de ${job.title}.`,
        detail: "El progreso actual se pierde y la consola puede quedar a mitad de un paquete. ¿Continuar?",
        confirmLabel: "Cancelar trabajo",
        danger: true,
      });
      if (!confirmed) return;
    }
    if (button) button.disabled = true;
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/${action}`, { method: "POST" });
      const messages = {
        pause: "Trabajo pausado.",
        resume: "Trabajo reanudado.",
        cancel: "Trabajo cancelado.",
        retry: "Trabajo reencolado.",
      };
      toast(messages[action] || "Trabajo actualizado.", "success");
      await refreshJobs({ silent: true });
    } catch (error) {
      toast(error.message || "No se pudo actualizar el trabajo.", "error");
      if (button) button.disabled = false;
    }
  }

  async function clearQueue(button = null) {
    const consoleItem = activeConsole();
    const useAll = state.queueFilter === "all" || !consoleItem;
    const scoped = useAll
      ? state.jobs
      : state.jobs.filter((job) => job.console_id === consoleItem.id);
    const terminal = scoped.filter((job) => TERMINAL_STATES.has(job.state));
    if (!terminal.length || state.clearingQueue) return;
    const noun = terminal.length === 1 ? "trabajo" : "trabajos";
    const scopeLabel = useAll ? "todas las consolas" : consoleItem.name;
    const confirmed = await askConfirm({
      kicker: "Cola de trabajos",
      title: "Limpiar trabajos terminados",
      message: `Se van a quitar ${terminal.length} ${noun} de ${scopeLabel}.`,
      detail: "Solo se eliminan los trabajos terminados, fallidos o cancelados. Los que están en curso no se tocan.",
      confirmLabel: "Limpiar cola",
      danger: true,
    });
    if (!confirmed) return;

    state.clearingQueue = true;
    if (button) button.disabled = true;
    const removedIds = new Set(terminal.map((job) => job.id));
    try {
      const data = await api("/api/jobs/clear", {
        method: "POST",
        body: useAll ? {} : { console_id: consoleItem.id },
      });
      const removed = Number(data.removed) || 0;
      state.jobs = state.jobs.filter((job) => !removedIds.has(job.id));
      renderQueue();
      renderConsoles();
      renderTopbar();
      toast(
        removed
          ? `${removed} ${removed === 1 ? "trabajo eliminado" : "trabajos eliminados"} de la cola.`
          : "No había trabajos terminados para limpiar.",
        removed ? "success" : "info",
      );
      await refreshJobs({ silent: true });
    } catch (error) {
      toast(error.message || "No se pudo limpiar la cola.", "error");
    } finally {
      state.clearingQueue = false;
      renderQueue();
    }
  }

  function selectConsole(consoleId) {
    if (!state.consoles.some((item) => item.id === consoleId)) return;
    closeRail();
    if (state.activeConsoleId === consoleId) {
      renderQueue();
      return;
    }
    state.activeConsoleId = consoleId;
    persistActiveConsole();
    renderConsoles();
    renderTopbar();
    renderSelection();
    renderHero();
    renderQueue();
    if (state.files) renderFiles();
    refreshStatuses().catch(() => {});
  }

  function openConsoleDialog(consoleId = null) {
    const consoleItem = consoleId
      ? state.consoles.find((item) => item.id === consoleId)
      : null;
    state.editingConsoleId = consoleItem ? consoleItem.id : null;
    dom.consoleForm.reset();
    dom.consoleFormError.textContent = "";
    dom.consoleName.value = consoleItem ? consoleItem.name : "";
    dom.consoleIp.value = consoleItem ? consoleItem.ip : "";
    dom.consoleRpiPort.value = String(consoleItem ? consoleItem.rpi_port : 12800);
    dom.consoleFtpPort.value = String(consoleItem ? consoleItem.ftp_port : 2121);
    dom.consoleFirmware.value = consoleItem ? consoleItem.firmware : "";
    dom.consoleNotes.value = consoleItem ? consoleItem.notes : "";
    dom.consoleDialogTitle.textContent = consoleItem ? "Editar consola" : "Agregar PS4";
    dom.btnSaveConsole.textContent = consoleItem ? "Guardar cambios" : "Agregar consola";
    dom.btnDeleteConsole.hidden = !consoleItem;
    dom.btnPrepareRpi.hidden = !consoleItem;
    dom.discoveryResults.innerHTML = "";
    if (dom.consoleDialog.open) dom.consoleDialog.close();
    dom.consoleDialog.showModal();
    window.setTimeout(() => dom.consoleName.focus(), 30);
  }

  function closeConsoleDialog() {
    if (dom.consoleDialog.open) dom.consoleDialog.close();
    state.editingConsoleId = null;
    dom.consoleFormError.textContent = "";
  }

  async function saveConsole(event) {
    event.preventDefault();
    dom.consoleFormError.textContent = "";
    if (!dom.consoleForm.checkValidity()) {
      dom.consoleForm.reportValidity();
      return;
    }
    const payload = {
      name: dom.consoleName.value.trim(),
      ip: dom.consoleIp.value.trim(),
      rpi_port: Number(dom.consoleRpiPort.value) || 12800,
      ftp_port: Number(dom.consoleFtpPort.value) || 2121,
      firmware: dom.consoleFirmware.value.trim(),
      notes: dom.consoleNotes.value.trim(),
    };
    const originalLabel = dom.btnSaveConsole.textContent;
    dom.btnSaveConsole.disabled = true;
    dom.btnSaveConsole.textContent = "Guardando…";
    try {
      const editingId = state.editingConsoleId;
      const data = editingId
        ? await api(`/api/consoles/${encodeURIComponent(editingId)}`, { method: "PUT", body: payload })
        : await api("/api/consoles", { method: "POST", body: payload });
      const saved = data.console;
      if (saved) {
        state.activeConsoleId = saved.id;
        persistActiveConsole();
      }
      closeConsoleDialog();
      await refreshConsoles();
      toast(editingId ? "Consola actualizada." : "Consola agregada.", "success");
      refreshStatuses(true).catch(() => {});
    } catch (error) {
      dom.consoleFormError.textContent = error.message || "No se pudo guardar la consola.";
    } finally {
      dom.btnSaveConsole.disabled = false;
      dom.btnSaveConsole.textContent = originalLabel;
    }
  }

  async function deleteConsole() {
    const consoleItem = state.consoles.find((item) => item.id === state.editingConsoleId);
    if (!consoleItem) return;
    const confirmed = await askConfirm({
      kicker: "Eliminar",
      title: "Quitar la consola",
      message: `Se va a eliminar ${consoleItem.name} de este servidor.`,
      detail: "No se borra ningún archivo de la biblioteca ni se toca la PS4. Los trabajos activos deben cancelarse antes.",
      confirmLabel: "Eliminar consola",
      danger: true,
    });
    if (!confirmed) return;
    const originalLabel = dom.btnDeleteConsole.textContent;
    dom.btnDeleteConsole.disabled = true;
    dom.btnDeleteConsole.textContent = "Eliminando…";
    try {
      await api(`/api/consoles/${encodeURIComponent(consoleItem.id)}`, { method: "DELETE" });
      closeConsoleDialog();
      if (state.activeConsoleId === consoleItem.id) state.activeConsoleId = null;
      persistActiveConsole();
      await refreshConsoles();
      toast("Consola eliminada.", "success");
    } catch (error) {
      dom.consoleFormError.textContent = error.message || "No se pudo eliminar la consola.";
      toast(error.message || "No se pudo eliminar la consola.", "error");
    } finally {
      dom.btnDeleteConsole.disabled = false;
      dom.btnDeleteConsole.textContent = originalLabel;
    }
  }

  async function prepareRpiConsole() {
    const consoleItem = state.consoles.find((item) => item.id === state.editingConsoleId);
    if (!consoleItem) return;
    const confirmed = await askConfirm({
      kicker: "Preparar consola",
      title: "Subir RPI por FTP",
      message: `Se va a subir el instalador Remote Package Installer a ${consoleItem.name}.`,
      detail: "Requiere que el servidor FTP de GoldHen esté activo. Después hay que abrir el instalador en la PS4.",
      confirmLabel: "Subir instalador",
    });
    if (!confirmed) return;
    const originalLabel = dom.btnPrepareRpi.textContent;
    dom.btnPrepareRpi.disabled = true;
    dom.btnPrepareRpi.textContent = "Subiendo…";
    try {
      const data = await api(`/api/consoles/${encodeURIComponent(consoleItem.id)}/prepare-rpi`, { method: "POST" });
      toast(data.message || "RPI subido por FTP.", "success", 12000);
      refreshStatuses(true).catch(() => {});
    } catch (error) {
      toast(error.message || "No se pudo subir RPI.", "error", 8000);
    } finally {
      dom.btnPrepareRpi.disabled = false;
      dom.btnPrepareRpi.textContent = originalLabel;
    }
  }

  async function discoverConsoles() {
    if (dom.btnDiscoverConsole.disabled) return;
    dom.btnDiscoverConsole.disabled = true;
    dom.discoveryResults.innerHTML = '<span class="discovery-empty"><span class="spinner"></span> Buscando en la red…</span>';
    try {
      const data = await api("/api/discover");
      const candidates = Array.isArray(data.candidates) ? data.candidates : [];
      if (!candidates.length) {
        dom.discoveryResults.innerHTML = `<span class="discovery-empty">No se encontró nada en ${esc(data.subnet || "la subred")}. Verificá que la PS4 esté encendida, con GoldHen y en la misma red.</span>`;
        return;
      }
      dom.discoveryResults.innerHTML = candidates.map((candidate) => {
        const ports = Array.isArray(candidate.ports) ? candidate.ports : [];
        const rpi = ports.includes(12801) ? 12801 : ports.includes(12800) ? 12800 : "";
        const ftp = ports.includes(2121) ? 2121 : "";
        return `<button class="discovery-item" type="button" data-discovery-ip="${esc(candidate.ip)}" data-rpi-port="${esc(rpi)}" data-ftp-port="${esc(ftp)}">
          <span>${esc(candidate.ip)}</span>
          <span class="ports">${esc(ports.join(" · "))}</span>
          ${candidate.likely_ps4 ? '<span class="badge kind-base">PS4?</span>' : ""}
        </button>`;
      }).join("");
    } catch (error) {
      dom.discoveryResults.innerHTML = `<span class="discovery-empty">${esc(error.message || "Falló la búsqueda.")}</span>`;
    } finally {
      dom.btnDiscoverConsole.disabled = false;
    }
  }

  function useDiscoveryCandidate(button) {
    const ip = button.dataset.discoveryIp || "";
    if (!ip) return;
    dom.consoleIp.value = ip;
    if (!dom.consoleName.value.trim()) {
      dom.consoleName.value = `PS4 ${ip.split(".").pop() || ""}`.trim();
    }
    if (button.dataset.rpiPort) dom.consoleRpiPort.value = button.dataset.rpiPort;
    if (button.dataset.ftpPort) dom.consoleFtpPort.value = button.dataset.ftpPort;
    dom.consoleFormError.textContent = "";
    dom.consoleFirmware.focus();
    toast("Datos de red copiados. Completá el firmware y guardá.", "success");
  }

  async function logout() {
    try {
      await api("/api/logout", { method: "POST" });
    } catch {
      /* redirect even if the session is already gone */
    }
    location.replace("/login");
  }

  function toggleSelection(path) {
    if (state.selected.has(path)) state.selected.delete(path);
    else state.selected.add(path);
    updateSelectionUI();
  }

  function bindEvents() {
    if (state.eventsBound) return;
    state.eventsBound = true;

    dom.btnAddConsole.addEventListener("click", () => openConsoleDialog());
    dom.btnDiscover.addEventListener("click", async () => {
      openConsoleDialog();
      await discoverConsoles();
    });
    dom.btnMenu.addEventListener("click", () => {
      if (dom.app.classList.contains("rail-open")) closeRail();
      else openRail();
    });
    dom.railBackdrop.addEventListener("click", closeRail);
    dom.btnLogout.addEventListener("click", logout);
    dom.btnQueue.addEventListener("click", openQueueDrawer);
    dom.diskAlertAction.addEventListener("click", () => navigate(""));
    dom.btnConsoleEdit.addEventListener("click", () => {
      openConsoleDialog(state.activeConsoleId);
    });

    dom.consoleList.addEventListener("click", (event) => {
      const edit = event.target.closest("[data-edit-console]");
      if (edit) {
        event.stopPropagation();
        openConsoleDialog(edit.dataset.editConsole);
        return;
      }
      const card = event.target.closest("[data-console-id]");
      if (card) selectConsole(card.dataset.consoleId);
    });
    dom.consoleList.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      if (event.target.closest("button")) return;
      const card = event.target.closest("[data-console-id]");
      if (!card) return;
      event.preventDefault();
      selectConsole(card.dataset.consoleId);
    });

    document.querySelectorAll("[data-close-drawer]").forEach((button) => {
      button.addEventListener("click", () => closeDrawer(button.dataset.closeDrawer));
    });
    dom.drawerBackdrop.addEventListener("click", closeDrawers);

    dom.queueFilterConsole.addEventListener("click", () => {
      state.queueFilter = "console";
      try { localStorage.setItem("ps4web.queueAll", "0"); } catch { /* ignore */ }
      renderQueue();
    });
    dom.queueFilterAll.addEventListener("click", () => {
      state.queueFilter = "all";
      try { localStorage.setItem("ps4web.queueAll", "1"); } catch { /* ignore */ }
      renderQueue();
    });
    dom.btnClearQueue.addEventListener("click", () => clearQueue(dom.btnClearQueue));
    dom.queueList.addEventListener("click", (event) => {
      const button = event.target.closest("[data-job-action]");
      if (!button) return;
      jobAction(button.dataset.jobId, button.dataset.jobAction, button);
    });

    dom.detailBody.addEventListener("click", (event) => {
      const install = event.target.closest("[data-detail-install]");
      if (install) {
        installPaths([install.dataset.detailInstall]);
        return;
      }
      const extract = event.target.closest("[data-detail-extract]");
      if (extract) {
        extractArchive(extract.dataset.detailExtract);
        return;
      }
      const select = event.target.closest("[data-detail-select]");
      if (select) {
        toggleSelection(select.dataset.detailSelect);
        select.textContent = state.selected.has(select.dataset.detailSelect)
          ? "Quitar de selección"
          : "Agregar a selección";
        return;
      }
      const remove = event.target.closest("[data-detail-delete]");
      if (remove) {
        deletePaths([remove.dataset.detailDelete]);
      }
    });

    dom.fileList.addEventListener("click", (event) => {
      const open = event.target.closest("[data-open-path]");
      if (open) {
        event.preventDefault();
        openEntry(findEntry(open.dataset.openPath));
        return;
      }
      const install = event.target.closest("[data-install-path]");
      if (install) {
        event.preventDefault();
        installPaths([install.dataset.installPath], install);
        return;
      }
      const extract = event.target.closest("[data-extract-path]");
      if (extract) {
        event.preventDefault();
        extractArchive(extract.dataset.extractPath, extract);
        return;
      }
      const remove = event.target.closest("[data-delete-path]");
      if (remove) {
        event.preventDefault();
        deletePaths([remove.dataset.deletePath], remove);
        return;
      }
      if (event.target.closest("button, input, label, a")) return;
      const row = event.target.closest(".file-row");
      if (row) openEntry(findEntry(row.dataset.path));
    });
    dom.fileList.addEventListener("change", (event) => {
      const checkbox = event.target.closest("[data-select-path]");
      if (!checkbox) return;
      const path = checkbox.dataset.selectPath;
      if (checkbox.checked) state.selected.add(path);
      else state.selected.delete(path);
      updateSelectionUI();
    });

    dom.search.addEventListener("input", () => {
      state.search = dom.search.value;
      renderFiles();
    });
    dom.selectAll.addEventListener("change", () => {
      const visible = getVisibleEntries().filter(canSelectEntry);
      if (dom.selectAll.checked) visible.forEach((entry) => state.selected.add(entry.path));
      else visible.forEach((entry) => state.selected.delete(entry.path));
      updateSelectionUI();
    });
    dom.includeDlc.addEventListener("change", () => {
      state.includeDlc = dom.includeDlc.checked;
      try { localStorage.setItem("ps4web.includeDlc", state.includeDlc ? "1" : "0"); } catch { /* ignore */ }
    });
    dom.btnInstall.addEventListener("click", () => {
      installPaths([...state.selected]);
    });
    dom.btnDeleteSelected.addEventListener("click", () => {
      deletePaths([...state.selected]);
    });
    dom.btnRefresh.addEventListener("click", () => {
      loadFiles(state.path, { push: false, keepSearch: true, keepSelection: true });
    });
    dom.breadcrumb.addEventListener("click", (event) => {
      const button = event.target.closest("[data-crumb]");
      if (!button || button.getAttribute("aria-current") === "page") return;
      navigate(button.dataset.crumb);
    });

    dom.consoleForm.addEventListener("submit", saveConsole);
    document.querySelectorAll("[data-close-modal=\"console\"]").forEach((button) => {
      button.addEventListener("click", closeConsoleDialog);
    });
    dom.btnDeleteConsole.addEventListener("click", deleteConsole);
    dom.btnPrepareRpi.addEventListener("click", prepareRpiConsole);
    dom.btnDiscoverConsole.addEventListener("click", discoverConsoles);
    dom.discoveryResults.addEventListener("click", (event) => {
      const button = event.target.closest("[data-discovery-ip]");
      if (button) useDiscoveryCandidate(button);
    });

    dom.confirmAccept.addEventListener("click", () => settleConfirm(true));
    dom.confirmCancel.addEventListener("click", () => settleConfirm(false));
    const confirmAlt = document.getElementById("confirm-alt");
    if (confirmAlt) confirmAlt.addEventListener("click", () => settleConfirm("alt"));
    dom.confirmDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      settleConfirm(false);
    });

    window.addEventListener("popstate", (event) => {
      const path = event.state && typeof event.state.ps4path === "string"
        ? event.state.ps4path
        : new URLSearchParams(location.search).get("path") || "";
      loadFiles(path, { push: false });
    });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!dom.consoleDialog.open && !dom.confirmDialog.open) {
        closeDrawers();
        closeRail();
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      refreshJobs({ silent: true }).catch(() => {});
      if (state.consoles.length) refreshStatuses().catch(() => {});
    });
  }

  boot();
})();
