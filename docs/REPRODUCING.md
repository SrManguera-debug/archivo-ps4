# Reproducir Archivo PS4 en otra máquina

Esta guía sirve para levantar el proyecto en otra máquina y reproducir el
problema conocido: **el panel marca una instalación como `done` antes de que la
PS4 termine de instalar**.

El repositorio no incluye PKGs, credenciales, configuración de producción ni el
estado de `/var/lib/ps4web`. Para una prueba real necesitás tus propios PKGs.

## Requisitos

- Linux con Python 3.11 o superior.
- `git`.
- Para extracción: `7z` o `7zz` (opcional).
- Para instalaciones reales:
  - PS4 y servidor en la misma LAN.
  - GoldHen y Remote Package Installer (RPI) activos en la PS4.
  - nginx sirviendo la carpeta de PKGs por HTTP.
  - Suficiente espacio libre en la PS4.

## 1. Clonar y ejecutar los tests

```bash
git clone <URL-del-repositorio> archivo-ps4
cd archivo-ps4
python3 -m unittest discover -s tests -v
```

La suite no necesita red externa ni una PS4. Debe terminar con `OK`.

## 2. Levantar el panel en modo desarrollo

```bash
mkdir -p /tmp/ps4web-dev/library /tmp/ps4web-dev/state

python3 -m ps4web init-config \
  --path /tmp/ps4web-dev/config.json \
  --library-root /tmp/ps4web-dev/library \
  --public-url http://127.0.0.1:8081 \
  --listen-host 127.0.0.1 \
  --listen-port 8081 \
  --state-dir /tmp/ps4web-dev/state

python3 -m ps4web serve --config /tmp/ps4web-dev/config.json
```

El comando `init-config` imprime la contraseña inicial. Abrí
`http://127.0.0.1:8081` e ingresá con el usuario `admin`.

Sin una PS4, el panel sirve para probar navegación, cola, extracción y la
interfaz. Para simular RPI podés usar `tests/fake_rpi.py`, pero ese simulador
**no reproduce el fallo real** porque devuelve un progreso idealizado.

## 3. Reproducir con una PS4 real

1. Copiá los PKGs a una carpeta servida por nginx.
2. Configurá `public_base_url` con la URL de nginx, no con el puerto del panel.
3. Iniciá RPI en la PS4 y agregá la consola en el panel con su IP.
4. Lanzá una instalación grande desde el panel.
5. Anotá el `task_id` del trabajo:

   ```bash
   curl -s http://127.0.0.1:8081/api/jobs
   ```

   También aparece en `jobs.json` dentro de `state_dir`.

6. En otra terminal, guardá las respuestas crudas de RPI:

   ```bash
   TASK_ID=123
   PS4_IP=192.168.1.50

   while true; do
     date -Is
     curl -sS --max-time 10 \
       -X POST "http://$PS4_IP:12800/api/get_task_progress" \
       -H 'Content-Type: application/json' \
       -d "{\"task_id\":$TASK_ID}"
     echo
     sleep 1
   done | tee /tmp/rpi-progress.log
   ```

7. Mientras la PS4 sigue mostrando “Instalando”, mirá el panel. El fallo se
   reproduce cuando el trabajo pasa a **Listo** antes de que la consola termine.
8. Guardá también los logs del servicio:

   ```bash
   sudo journalctl -u ps4web -n 300 --no-pager > /tmp/ps4web.log
   ```

## 4. Qué datos adjuntar al reporte

- Versión de firmware de la PS4 y versión de RPI.
- Tamaño y tipo de PKG (base, update o DLC).
- Respuestas crudas de `/api/get_task_progress` inmediatamente antes y después
  del falso `done`.
- Valores de `status`, `transferred_total`, `length_total`,
  `local_copy_percent`, `preparing_percent`, `num_index`, `num_total`,
  `rest_sec_total`, `error` y `bits`.
- Captura del panel y de la notificación de la PS4 en el mismo momento.
- Logs de `ps4web`.

Con esos datos se puede agregar un caso de regresión a `tests/fake_rpi.py` y
ajustar la detección de finalización en `ps4web/jobs.py` y
`ps4web/rpi.py`.
