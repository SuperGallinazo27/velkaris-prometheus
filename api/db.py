"""Synchronous SQLAlchemy engine: PostgreSQL through psycopg 3; SQLite only for tests."""

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from api.models import Base


def normalize_database_url(url: str) -> str:
    """postgres:// and postgresql:// select the psycopg 3 driver; SQLite URLs are kept as they are."""
    url = url.strip()
    scheme, separator, rest = url.partition("://")
    if separator and scheme in {"postgres", "postgresql", "postgresql+psycopg"}:
        return "postgresql+psycopg://" + rest
    if separator and scheme.split("+")[0] == "sqlite":
        return url
    # Never echo the value: it carries the database password.
    raise ValueError("DATABASE_URL debe ser una URL postgresql://.")


def _sqlite_foreign_keys(connection, _record):
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def build_engine(database_url: str) -> Engine:
    url = normalize_database_url(database_url)
    try:
        if url.startswith("sqlite"):
            # One shared connection keeps an in-memory database alive across threads.
            engine = create_engine(url, poolclass=StaticPool, connect_args={"check_same_thread": False})
            event.listen(engine, "connect", _sqlite_foreign_keys)
            return engine
        # Neon's pooled endpoint is PgBouncer in transaction mode, where server-side prepared
        # statements break. The connect timeout keeps an unreachable host from stalling startup.
        connect_args = {"prepare_threshold": None}
        if "connect_timeout=" not in url:
            connect_args["connect_timeout"] = 10
        return create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=2, pool_recycle=300,
                             hide_parameters=True, connect_args=connect_args)
    except (SQLAlchemyError, ValueError):
        # "from None": the parser's own message may quote parts of the URL.
        raise ValueError("DATABASE_URL no es una URL de base de datos utilizable.") from None


def init_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def error_summary(exc: SQLAlchemyError) -> str:
    """Class and SQLSTATE only: driver messages can name hosts, users or statements."""
    state = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return f"{type(exc).__name__}, SQLSTATE {state}" if state else type(exc).__name__
