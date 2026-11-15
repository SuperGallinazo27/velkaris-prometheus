"""Account use cases: sessions, temporary-password onboarding and admin CRUD.

scrypt takes about a second, so it never runs inside a transaction. Every operation that hashes
therefore re-reads the row under a lock afterwards and writes in one transaction: a reset,
registration or deletion that finished in the meantime is never overwritten or undone.
"""

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from api.account_schemas import Profile, ProfileInput, UserResponse
from api.config import Settings
from api.db import error_summary, init_schema
from api.errors import AnalysisError
from api.models import ACTIVE, ADMIN, AGENCY, PENDING, AuthSession, User, utcnow
from api.security import (
    MAX_EMAIL_LENGTH, MAX_PASSWORD_LENGTH, LoginThrottle, generate_temporary_password, hash_password,
    new_token, normalize_email, token_digest, verify_password,
)

logger = logging.getLogger(__name__)
DATABASE_UNAVAILABLE = "La base de datos de cuentas no está disponible. Inténtelo de nuevo en unos minutos."
PROFILE_FIELDS = ("full_name", "position", "agency", "acronym", "country")
REQUIRED_PROFILE_FIELDS = ("full_name", "agency", "country")
USER_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
LAST_USED_RESOLUTION = timedelta(minutes=1)
PREPARE_RETRY_SECONDS = 15.0
_prepare_lock = threading.Lock()


class Identity(NamedTuple):
    user: User
    session: AuthSession


def iso(value: datetime | None) -> str | None:
    """UTC, milliseconds and Z: the format of JavaScript's Date.toISOString()."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def user_response(user: User) -> UserResponse:
    values = [getattr(user, name) for name in PROFILE_FIELDS]
    profile = None if all(value is None for value in values) else Profile(
        **{name: value or "" for name, value in zip(PROFILE_FIELDS, values)})
    return UserResponse(id=user.id, email=user.email, role=user.role, status=user.status, profile=profile,
                        created_at=iso(user.created_at), registered_at=iso(user.registered_at),
                        temporary_expires_at=iso(user.temporary_expires_at))


def invalid_credentials() -> AnalysisError:
    return AnalysisError(401, "invalid_credentials", "Correo o contraseña incorrectos.")


def not_authenticated() -> AnalysisError:
    return AnalysisError(401, "not_authenticated", "Inicie sesión para continuar.")


def user_not_found() -> AnalysisError:
    return AnalysisError(404, "user_not_found", "El usuario no existe.")


def email_exists() -> AnalysisError:
    return AnalysisError(409, "email_exists", "Ya existe una cuenta con ese correo.")


def not_allowed_for_admin() -> AnalysisError:
    return AnalysisError(409, "not_allowed_for_admin", "Las cuentas de administrador no usan contraseñas temporales.")


def checked_email(value: str) -> str:
    email = normalize_email(value)
    if email is None:
        raise AnalysisError(422, "invalid_email", "El correo no tiene un formato válido.")
    return email


def check_password(password: str, settings: Settings) -> None:
    if len(password) < settings.min_password_length:
        raise AnalysisError(422, "password_too_short",
                            f"La contraseña debe tener al menos {settings.min_password_length} caracteres.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AnalysisError(422, "password_too_long",
                            f"La contraseña no puede superar {MAX_PASSWORD_LENGTH} caracteres.")


def temporary_password(supplied: str | None, settings: Settings) -> str:
    if supplied is None:
        return generate_temporary_password()
    check_password(supplied, settings)
    return supplied


def profile_values(profile: ProfileInput) -> dict[str, str | None]:
    values = {name: (getattr(profile, name) or "").strip() or None for name in PROFILE_FIELDS}
    if any(values[name] is None for name in REQUIRED_PROFILE_FIELDS):
        raise AnalysisError(422, "incomplete_profile", "Complete el nombre, la agencia y el país.")
    return values


def locked(db: Session, user_id: str) -> User | None:
    """The row as it is now, locked until this transaction ends (SQLite ignores the lock)."""
    return db.scalar(select(User).where(User.id == user_id).with_for_update()
                     .execution_options(populate_existing=True))


def find_user(db: Session, user_id: str, *, lock: bool = False) -> User:
    user = None
    if USER_ID.fullmatch(user_id):  # anything else never reaches the database: it does not exist
        user = locked(db, user_id) if lock else db.get(User, user_id)
    if user is None:
        raise user_not_found()
    return user


def end_sessions(db: Session, user_id: str) -> None:
    db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))


def release(db: Session) -> None:
    """End the read transaction so no pooled connection stays checked out while scrypt runs.
    Loaded objects stay readable: the session factory does not expire them on commit."""
    db.commit()


def login(db: Session, settings: Settings, throttle: LoginThrottle, email: str, password: str,
          client: str) -> tuple[str, AuthSession, User]:
    address = email.strip().lower()
    key = (address[:MAX_EMAIL_LENGTH], client)
    if not throttle.reserve(key):
        raise AnalysisError(429, "too_many_attempts",
                            "Demasiados intentos fallidos. Espere unos minutos antes de volver a intentarlo.")
    try:
        opened = open_session(db, settings, address, password)
    except AnalysisError as exc:
        if exc.code != "invalid_credentials":
            throttle.release(key)  # a correct but expired temporary password is not a failed attempt
        raise
    except Exception:
        throttle.release(key)  # no verdict on the credentials: outage or abandoned request
        raise
    throttle.clear(key)
    return opened


def open_session(db: Session, settings: Settings, address: str, password: str) -> tuple[str, AuthSession, User]:
    user = db.scalar(select(User).where(User.email == address)) if normalize_email(address) else None
    stored = user.password_hash if user else None
    release(db)
    # An unknown address costs the same scrypt verification, against a dummy hash.
    if not verify_password(password, stored):
        raise invalid_credentials()
    # The password was checked against what the row held before hashing: it must still hold it.
    user = locked(db, user.id)
    if user is None or user.password_hash != stored:
        raise invalid_credentials()
    now = utcnow()
    temporary = user.status == PENDING and user.temporary_expires_at is not None
    if temporary and user.temporary_expires_at <= now:
        raise AnalysisError(401, "temporary_password_expired",
                            "La contraseña temporal venció. Solicite una nueva al administrador.")
    expires_at = now + timedelta(hours=settings.session_hours)
    if temporary:
        # Nothing obtained with a temporary password outlives it.
        expires_at = min(expires_at, user.temporary_expires_at)
    token = new_token()
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.expires_at <= now))
    session = AuthSession(user_id=user.id, token_hash=token_digest(token), created_at=now,
                          expires_at=expires_at, last_used_at=now)
    db.add(session)
    db.commit()
    return token, session, user


def authenticate(db: Session, token: str | None) -> Identity:
    row = None
    if token and len(token) <= 128:
        row = db.execute(select(User, AuthSession).join(AuthSession, AuthSession.user_id == User.id)
                         .where(AuthSession.token_hash == token_digest(token))).first()
    if row is None:
        raise not_authenticated()
    current = Identity(*row)
    now = utcnow()
    if current.session.expires_at <= now:
        raise AnalysisError(401, "session_expired", "La sesión expiró. Inicie sesión de nuevo.")
    if now - current.session.last_used_at >= LAST_USED_RESOLUTION:
        # At most one write per minute and session. No row means it was closed a moment ago.
        touched = db.execute(update(AuthSession).where(AuthSession.id == current.session.id)
                             .values(last_used_at=now))
        db.commit()
        if not touched.rowcount:
            raise not_authenticated()
    return current


def logout(db: Session, session: AuthSession) -> None:
    db.execute(delete(AuthSession).where(AuthSession.id == session.id))
    db.commit()


def complete_registration(db: Session, settings: Settings, user: User, password: str,
                          profile: ProfileInput) -> None:
    if user.status != PENDING:
        raise AnalysisError(409, "not_pending", "Esta cuenta ya completó su registro.")
    check_password(password, settings)
    values = profile_values(profile)
    stored = user.password_hash
    release(db)
    if verify_password(password, stored):
        raise AnalysisError(422, "password_same_as_temporary",
                            "La nueva contraseña debe ser distinta de la temporal.")
    password_hash = hash_password(password)
    # A reset, another registration or a deletion that finished while this request was hashing
    # also closed the session it came with.
    user = locked(db, user.id)
    if user is None or user.password_hash != stored or user.status != PENDING:
        raise not_authenticated()
    user.password_hash = password_hash
    for name, value in values.items():
        setattr(user, name, value)
    user.status, user.registered_at, user.temporary_expires_at = ACTIVE, utcnow(), None
    end_sessions(db, user.id)
    db.commit()


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(User.created_at, User.id)))


def create_user(db: Session, settings: Settings, email: str, supplied: str | None) -> tuple[User, str]:
    address = checked_email(email)
    password = temporary_password(supplied, settings)
    if db.scalar(select(User.id).where(User.email == address)) is not None:
        raise email_exists()
    release(db)
    now = utcnow()
    user = User(email=address, role=AGENCY, status=PENDING, password_hash=hash_password(password),
                temporary_expires_at=now + timedelta(days=settings.temporary_password_days),
                created_at=now, updated_at=now)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # the same address was created while this request was hashing
        db.rollback()
        raise email_exists() from None
    return user, password


def update_user(db: Session, user_id: str, email: str | None, profile: ProfileInput | None) -> User:
    user = find_user(db, user_id, lock=True)
    address = user.email if email is None else checked_email(email)
    values = {} if profile is None else profile_values(profile)
    if address != user.email and db.scalar(select(User.id).where(User.email == address)) is not None:
        raise email_exists()
    user.email = address
    for name, value in values.items():
        setattr(user, name, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise email_exists() from None
    return user


def delete_user(db: Session, actor: User, user_id: str) -> None:
    user = find_user(db, user_id)
    if user.id == actor.id:
        raise AnalysisError(409, "cannot_delete_self", "No puede eliminar su propia cuenta.")
    # One ordered statement locks the target and every admin: concurrent deletions serialise
    # without deadlocking and the admin count cannot change under this transaction.
    roles = dict(db.execute(select(User.id, User.role).where(or_(User.id == user.id, User.role == ADMIN))
                            .order_by(User.id).with_for_update()).all())
    if user.id not in roles:
        raise user_not_found()
    if roles[user.id] == ADMIN and list(roles.values()).count(ADMIN) <= 1:
        raise AnalysisError(409, "last_admin", "No se puede eliminar al último administrador.")
    end_sessions(db, user.id)
    db.execute(delete(User).where(User.id == user.id))
    db.commit()


def reset_temporary_password(db: Session, settings: Settings, user_id: str,
                             supplied: str | None) -> tuple[User, str]:
    if find_user(db, user_id).role == ADMIN:
        raise not_allowed_for_admin()
    password = temporary_password(supplied, settings)
    release(db)
    password_hash = hash_password(password)
    user = find_user(db, user_id, lock=True)  # it may have changed or disappeared while hashing
    if user.role == ADMIN:
        raise not_allowed_for_admin()
    user.password_hash = password_hash
    user.status = PENDING  # the profile stays; registration must be completed again
    user.temporary_expires_at = utcnow() + timedelta(days=settings.temporary_password_days)
    end_sessions(db, user.id)
    db.commit()
    return user, password


def ensure_admin(db: Session, email: str, password: str, *, reset: bool = False) -> str:
    """Returns created, unchanged, reset or conflict (the address belongs to a non-admin account)."""
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        now = utcnow()
        db.add(User(email=email, role=ADMIN, status=ACTIVE, password_hash=hash_password(password),
                    created_at=now, updated_at=now))
        db.commit()
        return "created"
    if user.role != ADMIN:
        return "conflict"
    if not reset:
        return "unchanged"
    user.password_hash = hash_password(password)
    end_sessions(db, user.id)
    db.commit()
    return "reset"


def seed_admin(db: Session, settings: Settings) -> None:
    """First start only: once any admin exists, ADMIN_PASSWORD is never applied again."""
    if not (settings.admin_email and settings.admin_password):
        return
    if db.scalar(select(User.id).where(User.role == ADMIN).limit(1)) is not None:
        return
    if ensure_admin(db, normalize_email(settings.admin_email), settings.admin_password) == "conflict":
        logger.warning("ADMIN_EMAIL pertenece a una cuenta que no es administradora; no se creó ningún administrador.")


def prepare(state) -> bool:
    """Create the tables and the first admin once per app. False, without raising, while the
    database is unreachable; attempts are spaced so an outage does not queue every request."""
    if state.accounts_ready:
        return True
    with _prepare_lock:
        if state.accounts_ready:
            return True
        if time.monotonic() < state.accounts_retry_at:
            return False
        try:
            init_schema(state.db_engine)
            with state.db_sessions() as db:
                seed_admin(db, state.settings)
        except SQLAlchemyError as exc:
            logger.warning("Cuentas no disponibles: la base de datos no respondió (%s).", error_summary(exc))
            state.accounts_retry_at = time.monotonic() + PREPARE_RETRY_SECONDS
            return False
        state.accounts_ready = True
        return True
