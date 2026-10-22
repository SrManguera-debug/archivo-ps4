# Archivo PS4

[![tests](https://github.com/SrManguera-debug/archivo-ps4/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/SrManguera-debug/archivo-ps4/actions/workflows/tests.yml)

Panel privado para explorar una biblioteca de PKGs de PS4 y enviarlos a una o
varias consolas por la red local usando **GoldHen + Remote Package Installer
(RPI)**. No usa USB ni discos externos: los PKGs se sirven con nginx y la PS4
los descarga desde el servidor.

La aplicación está hecha con Python 3 (solo stdlib) y HTML/CSS/JS sin build
step. No necesita pip en producción.

## Qué incluye

- Biblioteca navegable con búsqueda por título, Title ID o nombre de archivo.
- Portadas (`icon0.png`) y metadatos SFO leídos directamente de cada PKG.
- Vista de detalle con firmware mínimo, versión, categoría y Content ID.
- Selección múltiple y envío de lotes en orden **base → update → DLC**.
- Eliminación permanente de archivos y carpetas desde el explorador, individual
  o en lote, con confirmación y protección para trabajos activos.
- Varias PS4, cada una con su cola secuencial e indicadores RPI/FTP/Remote Play.
- Cola persistente con progreso, pausa, reanudación, cancelación y reintento.
- Limpieza de la cola por consola o global: quita solo trabajos terminados,
  fallidos o cancelados, sin tocar los activos.
- Alerta visible cuando quedan menos de 10 GB libres o el disco supera el 90%
  de uso.
- Extracción de `.rar`, `.7z` y `.zip` en el servidor con progreso y control de
  espacio; nunca sobrescribe sin una confirmación explícita.
- Avisos de firmware cuando el PKG declara un mínimo superior al de la consola.
- Descubrimiento de consolas en la subred y subida del instalador RPI por FTP.
- Autenticación con una cuenta, cookie HttpOnly, CSRF y rate limit de intentos.
- Diseño responsive, sin fuentes ni recursos externos.

## Requisitos

- Debian/Ubuntu con Python **3.11+**.
- nginx sirviendo la biblioteca (recomendado en el puerto 80).
- `7z`/`7zz` para extraer archivos comprimidos (`p7zip-full` en Debian).
- PS4 con GoldHen y RPI activo en el puerto **12800** (también se prueban
  12801 para variantes).
- La PS4 y el servidor en la misma LAN.

## Instalación rápida

Desde la raíz del proyecto, en el servidor:

```bash
sudo ./deploy/install.sh
```

El instalador:

1. Copia la aplicación a `/opt/ps4web`.
2. Deja la configuración en `/etc/ps4web/config.json` (permisos `600`).
3. Crea el estado persistente en `/var/lib/ps4web`.
4. Instala y arranca `ps4web.service` en el puerto `8081`.
5. Muestra una contraseña inicial una sola vez y la guarda en
   `/root/ps4web-initial-credentials.txt` para que no se pierda.

Si preferís definir la contraseña:

```bash
sudo PS4WEB_PASSWORD='una-clave-larga' ./deploy/install.sh
```

Variables opcionales del instalador:

| Variable | Default |
| --- | --- |
| `PS4WEB_PASSWORD` | se genera |
| `PS4WEB_PUBLIC_URL` | `http://192.168.1.20` |
| `PS4WEB_LIBRARY` | `/mnt/juegos_ps4/descargas` |
| `PS4WEB_RPI_PKG` | `.../_instaladores/Remote_Package_Installer_FLTZ00003.pkg` |
| `PS4WEB_STATE_DIR` | `/var/lib/ps4web` |

La `public_url` se usa para construir las URLs que recibe RPI. Debe ser la
dirección del **nginx** que sirve los PKGs, normalmente
`http://192.168.1.20` (puerto 80). El panel queda en
`http://192.168.1.20:8081`.

### nginx

La configuración típica ya es suficiente: un `root` apuntando a la biblioteca y
soporte de `Range` (nginx lo trae por defecto).

```nginx
server {
    listen 80 default_server;
    server_name _;
    root /mnt/juegos_ps4/descargas;
    location / { try_files $uri $uri/ =404; }
}
```

Comprobación de descarga parcial:

```bash
curl -I -H 'Range: bytes=0-1023' \
  'http://192.168.1.20/Street_Fighter_6/ejemplo.pkg'
# Debe responder 206 Partial Content
```

## Primer uso

1. Abrí `http://192.168.1.20:8081` e ingresá con el usuario `admin` y la
   contraseña inicial.
2. En la barra lateral, agregá una PS4 con su IP y firmware declarado. El
   firmware se usa solo para avisos; no se modifica la consola.
3. Verificá los LEDs **RPI**, **FTP** y **RP**. Para instalar hace falta RPI.
4. Navegá una carpeta, seleccioná uno o más PKGs y pulsá **Instalar**.
5. La cola aparece a la derecha. Desde ahí podés pausar, reanudar, cancelar o
   reintentar.
6. Si una carpeta solo contiene un `.rar`/`.7z`, usá **Extraer**. El progreso
   también aparece en la cola.

### RPI

El panel espera que RPI esté escuchando en la PS4. Si no está iniciado:

1. Abrí GoldHen y activá el servidor FTP si pensás usar la subida automática.
2. Iniciá Remote Package Installer en la PS4.
3. En el panel, editá la consola y usá **Subir instalador** para copiar el PKG
   de RPI por FTP al directorio `/data/pkg/`.
4. En la PS4, instalá ese PKG desde **Ajustes → Ajustes de depuración → Juego →
   Instalar paquete**.

RPI debe quedar corriendo en primer plano o como servicio; si se cierra, los
trabajos en curso pueden fallar.

## Cómo funciona

```text
Navegador ──:8081──> ps4web (Python) ──HTTP 12800──> PS4 GoldHen + RPI
                             │
                             └──URL──> nginx :80 ──> /mnt/juegos_ps4/descargas
```

- ps4web no descarga el PKG desde la LAN para enviarlo: entrega una URL de
  nginx a RPI y la PS4 descarga directamente. Por eso nginx y la biblioteca
  deben ser accesibles desde la consola.
- Cada consola tiene un worker secuencial. Un lote puede contener una carpeta:
  se resuelven los PKGs, se ordenan base → update → DLC y se omiten duplicados
  que ya estén activos para esa consola.
- El estado se escribe de forma atómica en `/var/lib/ps4web` y sobrevive
  reinicios. Los trabajos que estaban activos al reiniciar se marcan como
  error para no simular un progreso que ya no existe.
- La extracción ejecuta `7z` con una lista validada de miembros, exige al menos
  1 GiB libre adicional y se niega a pisar archivos existentes salvo que el
  usuario elija explícitamente **Extraer y sobrescribir**.

## Configuración

`/etc/ps4web/config.json`:

```json
{
  "library_root": "/mnt/juegos_ps4/descargas",
  "public_base_url": "http://192.168.1.20",
  "listen_host": "0.0.0.0",
  "listen_port": 8081,
  "subnet": "192.168.1.0/24",
  "state_dir": "/var/lib/ps4web",
  "cache_dir": "/var/lib/ps4web/cache",
  "auth_user": "admin",
  "auth_password_hash": "pbkdf2_sha256$...",
  "session_secret": "...",
  "rpi_pkg": "/mnt/juegos_ps4/descargas/_instaladores/Remote_Package_Installer_FLTZ00003.pkg",
  "scan_ports": [12800, 12801, 2121, 9295],
  "app_version": "1.0.0"
}
```

Después de editar el archivo:

```bash
sudo systemctl restart ps4web
```

Para cambiar la contraseña, generá un hash nuevo y reemplazá
`auth_password_hash`:

```bash
cd /opt/ps4web
sudo python3 -m ps4web hash-password
```

También podés regenerar una configuración completa (se pierde la anterior):

```bash
cd /opt/ps4web
sudo python3 -m ps4web init-config --force --path /etc/ps4web/config.json
```

## Operación

```bash
sudo systemctl status ps4web --no-pager
sudo journalctl -u ps4web -f
sudo systemctl restart ps4web
```

El servicio corre con `ProtectSystem=strict`; solo puede escribir en la
biblioteca y en `/var/lib/ps4web`. Se ejecuta como root porque la extracción
escribe dentro de la biblioteca y los archivos suelen pertenecer a root; las
rutas de escritura están limitadas en la unidad systemd.

## Seguridad

- La aplicación está pensada para una LAN privada. No la expongas a internet.
- La sesión usa cookie `HttpOnly` + `SameSite=Strict` y las mutaciones exigen el
  header `X-CSRF-Token`.
- Hay rate limit por IP para intentos de login.
- El servidor no sube los PKGs: nginx los sirve directamente. Si nginx tiene
  autenticación o restricciones, RPI también las necesitará.
- Cambiá las contraseñas de root, pyLoad, qBittorrent y cualquier servicio que
  haya quedado con credenciales débiles o compartidas.

## Pruebas

No requieren red externa:

```bash
python3 -m unittest discover -s tests -v
```

Para montar el proyecto en otra máquina y reproducir el problema de las
instalaciones que se marcan como terminadas antes de tiempo, seguí
[`docs/REPRODUCING.md`](docs/REPRODUCING.md).

Para una prueba manual del frontend con una RPI falsa:

```bash
python3 - <<'PY'
from tests.fake_rpi import FakeRpi
import time
fake = FakeRpi(port=12800).start()
print("fake RPI en", fake.port, flush=True)
time.sleep(3600)
PY
```

Luego iniciá el panel con una configuración de desarrollo y abrí el navegador.

## Solución de problemas

| Síntoma | Revisar |
| --- | --- |
| La PS4 no descarga | Que `public_base_url` sea la IP/puerto de nginx, que el archivo exista y que `curl -I` devuelva `206` con `Range`. |
| RPI no responde | GoldHen activo, RPI abierto y puerto `12800`/`12801` accesible desde el servidor. |
| Error al extraer | `7z` instalado, espacio libre suficiente (1 GiB de margen), permisos de escritura en la carpeta. |
| La cola no avanza | Mirá `journalctl -u ps4web`; RPI solo procesa un trabajo por consola. |
| No aparece una consola al descubrir | La subred configurada debe incluir la PS4; el descubrimiento solo soporta IPv4 y hasta 1024 direcciones. |
| El navegador conserva una versión vieja | El servidor manda `no-cache` para HTML/JS/CSS; recargá con Ctrl+Shift+R. |

## Estructura

```text
ps4web/
  config.py      configuración, PBKDF2 y CLI auxiliar
  auth.py        sesiones y CSRF
  pkg.py         parser de PKG, SFO e iconos
  library.py     navegación, caché y URLs de nginx
  consoles.py    CRUD, sondeo de puertos, descubrimiento, FTP
  rpi.py         cliente HTTP de Remote Package Installer
  jobs.py        colas, workers, progreso, extracción y persistencia
  server.py      HTTP/API, auth, archivos estáticos y seguridad
  static/        interfaz
deploy/
  install.sh     instalación idempotente
  ps4web.service unidad systemd endurecida
tests/           suite unittest + RPI/7z falsos
```

## Licencia

El código se distribuye bajo la licencia MIT; ver [`LICENSE`](LICENSE). Los
PKGs, portadas y marcas pertenecen a sus respectivos dueños y no forman parte
de este repositorio.
