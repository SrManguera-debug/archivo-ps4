"""Command line entry point for Archivo PS4."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import threading
from urllib.parse import urlparse

from .config import (
    APP_VERSION,
    DEFAULT_CONFIG_PATH,
    DEFAULT_LIBRARY_ROOT,
    DEFAULT_PUBLIC_BASE_URL,
    DEFAULT_RPI_PKG,
    DEFAULT_STATE_DIR,
    ConfigError,
    hash_password,
    init_config,
    load_config,
)
from .server import create_server

LOGGER = logging.getLogger("ps4web")


def _cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    server = create_server(config, host=args.host, port=args.port)
    app = server.app

    def stop(signum: int, frame: object) -> None:
        LOGGER.info("Señal %s; deteniendo", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOGGER.info(
        "Archivo PS4 escuchando en http://%s:%s (biblioteca %s)",
        args.host or config.listen_host,
        server.server_address[1],
        config.library_root,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.server_close()
        LOGGER.info("Servidor detenido")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    path = args.path or os.environ.get("PS4WEB_CONFIG") or DEFAULT_CONFIG_PATH
    try:
        config, password = init_config(
            path,
            library_root=args.library_root,
            public_base_url=args.public_url,
            password=args.password,
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            subnet=args.subnet,
            state_dir=args.state_dir,
            rpi_pkg=args.rpi_pkg,
            force=args.force,
        )
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"Configuración: {path}")
    print(f"Biblioteca:    {config.library_root}")
    print(f"Estado:        {config.state_dir}")
    public_url = config.public_base_url
    if not urlparse(public_url).port:
        public_url = f"{public_url}:{config.listen_port}"
    print(f"URL:           {public_url}")
    print(f"Usuario:       {config.auth_user}")
    print(f"Contraseña:    {password}")
    print("\nGuardá la contraseña; no se vuelve a mostrar.")
    return 0


def _cmd_hash(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("Contraseña: ")
    if not password:
        print("La contraseña no puede estar vacía", file=sys.stderr)
        return 2
    print(hash_password(password))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ps4web",
        description="Biblioteca e instalador de PKGs para PS4 por LAN",
    )
    parser.add_argument("--version", action="version", version=f"Archivo PS4 {APP_VERSION}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="iniciar el servidor web")
    serve.add_argument("--config", help="ruta del config.json")
    serve.add_argument("--host", help="dirección de escucha (default: la del config)")
    serve.add_argument("--port", type=int, help="puerto de escucha (default: el del config)")
    serve.set_defaults(func=_cmd_serve)

    init = sub.add_parser("init-config", help="crear configuración y contraseña")
    init.add_argument("--path", help="ruta del config.json")
    init.add_argument("--library-root", default=DEFAULT_LIBRARY_ROOT)
    init.add_argument("--public-url", default=DEFAULT_PUBLIC_BASE_URL)
    init.add_argument("--password", help="contraseña (si se omite se genera)")
    init.add_argument("--listen-host", default="0.0.0.0")
    init.add_argument("--listen-port", type=int, default=8081)
    init.add_argument("--subnet", default="192.168.1.0/24")
    init.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    init.add_argument("--rpi-pkg", default=DEFAULT_RPI_PKG)
    init.add_argument("--force", action="store_true", help="reemplazar una configuración existente")
    init.set_defaults(func=_cmd_init)

    hashed = sub.add_parser("hash-password", help="generar un hash PBKDF2")
    hashed.add_argument("--password", help="contraseña (si se omite se pregunta)")
    hashed.set_defaults(func=_cmd_hash)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
