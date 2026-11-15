"""Operator commands: python -m api.manage [--env-file PATH] {init-db | admin [--reset]}.

Prints status lines only; passwords and the database URL never reach the output.
"""

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from api.config import Settings
from api.db import build_engine, error_summary, init_schema
from api.security import normalize_email
from api.services import accounts

ADMIN_OUTCOMES = {
    "created": "Administrador creado: {}",
    "unchanged": "El administrador ya existe; no se modificó: {}",
    "reset": "Contraseña del administrador restablecida y sesiones cerradas: {}",
    "conflict": "El correo pertenece a una cuenta que no es administradora; no se modificó: {}",
}


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=VALUE lines and # comments only: no quoting, export prefixes or interpolation."""
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or any(char.isspace() for char in key):
            # The line may hold a secret: report only where it is.
            raise ValueError(f"Línea {number} del archivo de entorno: se esperaba CLAVE=VALOR.")
        values[key] = value.strip()
    return values


def load_env_file(path: Path) -> None:
    """Variables already present in the process environment win."""
    for key, value in parse_env_file(path.read_text(encoding="utf-8-sig")).items():
        os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.manage", description="Operaciones de las cuentas de PROMETHEUS.")
    parser.add_argument("--env-file", type=Path, metavar="PATH",
                        help="Archivo CLAVE=VALOR; no sustituye variables ya definidas en el entorno.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Crea las tablas de cuentas y de turnos si no existen.")
    admin = commands.add_parser("admin", help="Crea el administrador de ADMIN_EMAIL/ADMIN_PASSWORD si no existe.")
    admin.add_argument("--reset", action="store_true",
                       help="Aplica además ADMIN_PASSWORD a la cuenta existente y cierra sus sesiones.")
    options = parser.parse_args(argv)
    try:
        if options.env_file:
            load_env_file(options.env_file)
        settings = Settings.from_env()
        if not settings.database_url:
            raise ValueError("DATABASE_URL no está configurada.")
        if options.command == "admin" and not (settings.admin_email and settings.admin_password):
            raise ValueError("Este comando requiere ADMIN_EMAIL y ADMIN_PASSWORD.")
        engine = build_engine(settings.database_url)
    except (OSError, UnicodeDecodeError):
        print("No se pudo leer el archivo de entorno.", file=sys.stderr)
        return 2
    except ValueError as exc:  # raised by this project only, with messages free of secrets
        print(exc, file=sys.stderr)
        return 2
    try:
        init_schema(engine)
        if options.command == "init-db":
            print("Tablas listas: users, auth_sessions, reservations, place_overrides.")
            return 0
        email = normalize_email(settings.admin_email)
        with Session(engine) as db:
            outcome = accounts.ensure_admin(db, email, settings.admin_password, reset=options.reset)
        print(ADMIN_OUTCOMES[outcome].format(email))
        return 1 if outcome == "conflict" else 0
    except SQLAlchemyError as exc:
        print(f"Base de datos no disponible ({error_summary(exc)}).", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
