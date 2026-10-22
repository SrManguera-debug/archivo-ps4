#!/usr/bin/env bash
#
# Instalador de Archivo PS4 para Debian/Ubuntu.
# Ejecutar como root desde la raíz del proyecto o desde el tarball descomprimido:
#
#   sudo ./deploy/install.sh
#
# Variables opcionales:
#   PS4WEB_PASSWORD   contraseña del panel (si se omite se genera una)
#   PS4WEB_PUBLIC_URL base pública para los PKG (default http://192.168.1.20)
#   PS4WEB_LIBRARY    biblioteca de PKGs (default /mnt/juegos_ps4/descargas)
#   PS4WEB_RPI_PKG    instalador RPI (default dentro de la biblioteca)
#   PS4WEB_STATE_DIR  estado persistente (default /var/lib/ps4web)

set -euo pipefail

APP_DIR="/opt/ps4web"
CONFIG_DIR="/etc/ps4web"
CONFIG="$CONFIG_DIR/config.json"
SERVICE="/etc/systemd/system/ps4web.service"
STATE_DIR="${PS4WEB_STATE_DIR:-/var/lib/ps4web}"
LIBRARY="${PS4WEB_LIBRARY:-/mnt/juegos_ps4/descargas}"
PUBLIC_URL="${PS4WEB_PUBLIC_URL:-http://192.168.1.20}"
RPI_PKG="${PS4WEB_RPI_PKG:-$LIBRARY/_instaladores/Remote_Package_Installer_FLTZ00003.pkg}"
LISTEN_PORT="8081"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

log() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  fail "Este instalador debe ejecutarse como root."
fi

command -v python3 >/dev/null || fail "Falta python3."
python3 - <<'PY' || fail "Se requiere Python 3.11 o superior."
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

[ -d "$SRC_DIR/ps4web" ] || fail "No encuentro el directorio ps4web junto al instalador."
[ -d "$SRC_DIR/deploy" ] || fail "No encuentro el directorio deploy junto al instalador."
[ -d "$LIBRARY" ] || fail "No existe la biblioteca: $LIBRARY"

if ! command -v 7z >/dev/null 2>&1 && ! command -v 7zz >/dev/null 2>&1; then
  log "Instalando p7zip-full para la extracción de archivos"
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y p7zip-full
fi

command -v 7z >/dev/null 2>&1 || command -v 7zz >/dev/null 2>&1 \
  || fail "No hay un ejecutable 7z/7zz disponible."

log "Copiando la aplicación en $APP_DIR"
install -d -m 0755 "$APP_DIR"
rm -rf "$APP_DIR/ps4web"
cp -a "$SRC_DIR/ps4web" "$APP_DIR/ps4web"
find "$APP_DIR/ps4web" -type d -name __pycache__ -prune -exec rm -rf {} +
cp -a "$SRC_DIR/README.md" "$APP_DIR/README.md" 2>/dev/null || true

log "Creando directorios de configuración y estado"
install -d -m 0700 "$CONFIG_DIR" "$STATE_DIR"

if [ ! -f "$CONFIG" ]; then
  log "Creando configuración inicial"
  args=(
    init-config
    --path "$CONFIG"
    --library-root "$LIBRARY"
    --public-url "$PUBLIC_URL"
    --listen-host "0.0.0.0"
    --listen-port "$LISTEN_PORT"
    --subnet "192.168.1.0/24"
    --state-dir "$STATE_DIR"
    --rpi-pkg "$RPI_PKG"
  )
  if [ -n "${PS4WEB_PASSWORD:-}" ]; then
    args+=(--password "$PS4WEB_PASSWORD")
  fi
  (cd "$APP_DIR" && python3 -m ps4web "${args[@]}") | tee /root/ps4web-initial-credentials.txt
  chmod 600 /root/ps4web-initial-credentials.txt
  echo
  echo "Las credenciales iniciales quedaron en /root/ps4web-initial-credentials.txt"
  echo "Borrálas cuando las hayas guardado en un lugar seguro."
else
  chmod 600 "$CONFIG"
  echo "La configuración ya existe; se conserva $CONFIG"
fi

[ -f "$RPI_PKG" ] || echo "Aviso: todavía no existe el instalador RPI en $RPI_PKG"
if ! command -v nginx >/dev/null 2>&1; then
  echo "Aviso: nginx no está instalado; los PKGs no podrán descargarse desde la PS4."
else
  nginx -t >/dev/null 2>&1 || echo "Aviso: nginx tiene errores de configuración."
fi

log "Instalando el servicio systemd"
install -m 0644 "$SRC_DIR/deploy/ps4web.service" "$SERVICE"
systemctl daemon-reload
systemctl enable ps4web.service >/dev/null
systemctl restart ps4web.service

log "Esperando a que el panel responda en el puerto $LISTEN_PORT"
ready=0
for _ in $(seq 1 40); do
  if curl -fsS --max-time 2 -o /dev/null "http://127.0.0.1:$LISTEN_PORT/login"; then
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  systemctl status ps4web.service --no-pager || true
  journalctl -u ps4web.service -n 80 --no-pager || true
  fail "El servicio no respondió. Revisá el journal con: journalctl -u ps4web -n 100"
fi

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw allow "$LISTEN_PORT/tcp" >/dev/null || true
  echo "Regla ufw agregada para el puerto $LISTEN_PORT/tcp"
fi

echo
systemctl --no-pager --full status ps4web.service | sed -n '1,14p'
echo
echo "Panel:   http://$(hostname -I | awk '{print $1}'):$LISTEN_PORT"
echo "PKGs:    $PUBLIC_URL/<ruta>  (nginx, puerto 80)"
echo "Estado:  $STATE_DIR"
echo
echo "Verificá desde otra máquina de la LAN que el puerto $LISTEN_PORT esté permitido"
echo "por el firewall del LXC/proxmox, no solo por ufw."
