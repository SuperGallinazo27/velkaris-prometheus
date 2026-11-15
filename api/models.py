"""Account and booking tables. Datetimes are timezone-aware UTC in Python on every backend."""

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

ADMIN, AGENCY = "admin", "agencia"
PENDING, ACTIVE = "pendiente", "activo"
# Turnos (docs/TURNOS.md). A reservation is active while it is RESERVED; cancelling keeps the row.
RESERVED, CANCELLED = "reservado", "cancelado"
# Administrator decision on one place: it wins over the protected-area rule.
ENABLED, CLOSED = "habilitado", "cerrado"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator):
    """DateTime(timezone=True) that also survives SQLite, which stores and returns naive values."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else as_utc(value)

    def process_result_value(self, value, dialect):
        return None if value is None else as_utc(value)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True)  # trimmed and lower-cased
    role: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    password_hash: Mapped[str] = mapped_column(String(255))
    temporary_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    full_name: Mapped[str | None] = mapped_column(String(120))
    position: Mapped[str | None] = mapped_column(String(80))
    agency: Mapped[str | None] = mapped_column(String(120))
    acronym: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    registered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # SHA-256 hex; the token is never stored
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Reservation(Base):
    """One agency holds one place for a range of days (both ends included)."""

    __tablename__ = "reservations"
    __table_args__ = (Index("ix_reservations_place_dates", "place_id", "status", "start_date", "end_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(16), unique=True)  # short reference people can read out
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    place_id: Mapped[str] = mapped_column(String(64))  # id in data/lugares.json
    place_name: Mapped[str] = mapped_column(String(120))  # as it was when booked
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16))
    # Declared by the agency when the place lies inside a protected area; never verified here.
    permit_reference: Mapped[str | None] = mapped_column(String(200))
    # Snapshot of the campaign plan the booking came from, when there was one.
    mission: Mapped[str | None] = mapped_column(String(120))
    robot: Mapped[str | None] = mapped_column(String(80))
    people: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_by: Mapped[str | None] = mapped_column(String(16))  # role of whoever cancelled
    cancel_reason: Mapped[str | None] = mapped_column(String(300))


class PlaceOverride(Base):
    """The administrator closed a place, or enabled one that the protected-area rule blocks."""

    __tablename__ = "place_overrides"

    place_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(300))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
