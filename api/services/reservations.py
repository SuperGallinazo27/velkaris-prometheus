"""Booking use cases: place status, availability, reservations and the administrator's decisions.

A turno is a range of whole days (both ends included) in which one agency holds one place. It
coordinates the agencies of this portal; it is not a permit from the country, the landowner or a
protected area's authority.

Two requests for the same place are serialised with a transaction-level advisory lock on
PostgreSQL, taken before the overlap check, so they can never both find the place free. SQLite,
used only in tests, has a single writer.
"""

import re
import secrets
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from api.errors import AnalysisError
from api.models import ADMIN, CANCELLED, CLOSED, ENABLED, RESERVED, PlaceOverride, Reservation, User, utcnow
from api.reservation_schemas import (
    AgencyInfo, AreaResponse, AvailabilityResponse, BookingRequest, BusyRange, Limits, OverrideResponse,
    PlacesResponse, PlaceStatusResponse, ReservationList, ReservationResponse, SourceResponse,
)
from api.security import TEMPORARY_ALPHABET
from api.services import places
from api.services.accounts import iso
from api.services.places import FREE, NO_DATA, PERMIT, PROTECTED, Area, Catalog, Place

# Limits of a turno. The same numbers are shown by the frontend (GET /places returns them).
MAX_DAYS = 60
MAX_DAYS_AHEAD = 730  # about 24 months
MAX_ACTIVE_PER_AGENCY = 5
# The server counts days in UTC; a browser west of Greenwich can still be on the previous day.
PAST_TOLERANCE_DAYS = 1

RESERVATION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
          "octubre", "noviembre", "diciembre")
LIMITS = Limits(max_days=MAX_DAYS, max_days_ahead=MAX_DAYS_AHEAD, max_active_per_agency=MAX_ACTIVE_PER_AGENCY)


def today() -> date:
    return utcnow().date()


def spanish_range(start: date, end: date) -> str:
    """«del 3 al 12 de marzo de 2027», «del 28 de febrero al 4 de marzo de 2027», «el 3 de marzo de 2027»."""
    last = f"{end.day} de {MONTHS[end.month - 1]} de {end.year}"
    if start == end:
        return f"el {last}"
    if (start.year, start.month) == (end.year, end.month):
        return f"del {start.day} al {last}"
    first = f"{start.day} de {MONTHS[start.month - 1]}" + ("" if start.year == end.year else f" de {start.year}")
    return f"del {first} al {last}"


@dataclass(frozen=True)
class Status:
    """What a place is for booking once the administrator's decision is applied."""

    status: str
    bookable: bool
    permit_required: bool
    reason: str
    override: PlaceOverride | None


def effective(place: Place, override: PlaceOverride | None) -> Status:
    base = place.base_status
    if override is not None and override.mode == CLOSED:
        return Status(CLOSED, False, False, f"Cerrado por el administrador: {override.reason}", override)
    # Enabling only means something while the dataset blocks the place.
    if override is not None and override.mode == ENABLED and base in (PROTECTED, NO_DATA):
        blocked = (f"{places.inside(place)} (protección estricta)" if base == PROTECTED
                   else "No se pudo consultar en la base de áreas protegidas")
        return Status(ENABLED, True, True,
                      f"{blocked}, pero el administrador lo habilitó: {override.reason} "
                      "Para reservar hay que declarar el permiso.", override)
    return Status(base, base in (FREE, PERMIT), base == PERMIT, places.dataset_reason(place), None)


def place_not_found() -> AnalysisError:
    return AnalysisError(404, "place_not_found", "Ese lugar no está en el catálogo.")


def reservation_not_found() -> AnalysisError:
    return AnalysisError(404, "reservation_not_found", "Ese turno no existe.")


def find_place(catalog: Catalog, place_id: str) -> Place:
    place = catalog.places.get(place_id)
    if place is None:
        raise place_not_found()
    return place


def lock(db: Session, key: str) -> None:
    """Held until the transaction ends. Always taken in the same order: agency, then place."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})


def area_response(area: Area) -> AreaResponse:
    return AreaResponse(site_id=area.site_id, name=area.display, name_eng=area.name_eng, designation=area.designation,
                        designation_eng=area.designation_eng, designation_type=area.designation_type,
                        iucn=area.iucn, status=area.status, status_year=area.status_year,
                        governance=area.governance, authority=area.authority, iso3=area.iso3, strict=area.strict)


def status_response(place: Place, override: PlaceOverride | None, occupied: bool, active: int) -> PlaceStatusResponse:
    status = effective(place, override)
    return PlaceStatusResponse(
        id=place.id, name=place.name, province=place.province, country=place.country,
        status=status.status, bookable=status.bookable, permit_required=status.permit_required,
        reason=status.reason, dataset_status=place.base_status,
        areas=[area_response(area) for area in place.in_force],
        nearby_strict=[area_response(area) for area in place.nearby_strict],
        override=None if override is None else OverrideResponse(mode=override.mode, reason=override.reason,
                                                                updated_at=iso(override.updated_at)),
        occupied_today=occupied, active_reservations=active)


def source_response(catalog: Catalog) -> SourceResponse:
    source = catalog.source
    return SourceResponse(name=source.get("name", ""), url=source.get("url", ""), terms=source.get("terms", ""),
                          citation=source.get("citation", ""), release=source.get("release"),
                          fetched=source.get("fetched"))


def activity(db: Session, day: date, place_id: str | None = None) -> tuple[dict[str, int], set[str]]:
    """Per place: how many turnos are still active (not finished) and which are occupied on `day`."""
    query = select(Reservation.place_id, Reservation.start_date).where(
        Reservation.status == RESERVED, Reservation.end_date >= day)
    if place_id is not None:
        query = query.where(Reservation.place_id == place_id)
    counts: dict[str, int] = {}
    occupied: set[str] = set()
    for place, start in db.execute(query):
        counts[place] = counts.get(place, 0) + 1
        if start <= day:
            occupied.add(place)
    return counts, occupied


def list_places(db: Session, catalog: Catalog) -> PlacesResponse:
    day = today()
    overrides = {row.place_id: row for row in db.scalars(select(PlaceOverride))}
    counts, occupied = activity(db, day)
    return PlacesResponse(
        source=source_response(catalog), strict_categories=sorted(places.STRICT_IUCN), limits=LIMITS,
        today=day.isoformat(),
        places=[status_response(place, overrides.get(place.id), place.id in occupied, counts.get(place.id, 0))
                for place in catalog.places.values()])


def one_status(db: Session, place: Place) -> PlaceStatusResponse:
    counts, occupied = activity(db, today(), place.id)
    return status_response(place, db.get(PlaceOverride, place.id), place.id in occupied, counts.get(place.id, 0))


def agency_label(user: User) -> str:
    return user.agency or user.email


def availability(db: Session, catalog: Catalog, place_id: str, viewer: User) -> AvailabilityResponse:
    place = find_place(catalog, place_id)
    day = today()
    rows = db.execute(select(Reservation, User).join(User, User.id == Reservation.user_id).where(
        Reservation.place_id == place.id, Reservation.status == RESERVED, Reservation.end_date >= day)
        .order_by(Reservation.start_date)).all()
    busy = [BusyRange(start_date=reservation.start_date.isoformat(), end_date=reservation.end_date.isoformat(),
                      mine=reservation.user_id == viewer.id,
                      agency=agency_label(owner) if viewer.role == ADMIN else None)
            for reservation, owner in rows]
    return AvailabilityResponse(place=one_status(db, place), busy=busy, today=day.isoformat(), limits=LIMITS)


def phase(reservation: Reservation, day: date) -> str:
    if reservation.status == CANCELLED:
        return "cancelado"
    if day < reservation.start_date:
        return "proximo"
    return "en_curso" if day <= reservation.end_date else "terminado"


def reservation_response(reservation: Reservation, owner: User | None, day: date) -> ReservationResponse:
    return ReservationResponse(
        id=reservation.id, code=reservation.code, place_id=reservation.place_id, place_name=reservation.place_name,
        start_date=reservation.start_date.isoformat(), end_date=reservation.end_date.isoformat(),
        days=(reservation.end_date - reservation.start_date).days + 1, status=reservation.status,
        phase=phase(reservation, day), permit_reference=reservation.permit_reference, mission=reservation.mission,
        robot=reservation.robot, people=reservation.people, estimated_cost_usd=reservation.estimated_cost_usd,
        created_at=iso(reservation.created_at), cancelled_at=iso(reservation.cancelled_at),
        cancelled_by=reservation.cancelled_by, cancel_reason=reservation.cancel_reason,
        agency=None if owner is None else AgencyInfo(user_id=owner.id, email=owner.email, agency=owner.agency or "",
                                                     acronym=owner.acronym or "", country=owner.country or ""))


def list_mine(db: Session, user: User) -> ReservationList:
    day = today()
    rows = db.scalars(select(Reservation).where(Reservation.user_id == user.id)
                      .order_by(Reservation.start_date.desc(), Reservation.created_at.desc()))
    return ReservationList(today=day.isoformat(), reservations=[reservation_response(row, user, day) for row in rows])


def list_all(db: Session, status: str | None, place_id: str | None) -> ReservationList:
    day = today()
    query = select(Reservation, User).join(User, User.id == Reservation.user_id)
    if status is not None:
        query = query.where(Reservation.status == status)
    if place_id is not None:
        query = query.where(Reservation.place_id == place_id)
    rows = db.execute(query.order_by(Reservation.start_date, Reservation.created_at)).all()
    return ReservationList(today=day.isoformat(),
                           reservations=[reservation_response(reservation, owner, day) for reservation, owner in rows])


def invalid_dates(message: str) -> AnalysisError:
    return AnalysisError(422, "invalid_dates", message)


def parse_range(start_text: str, end_text: str) -> tuple[date, date]:
    try:
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    except ValueError:
        raise invalid_dates("Fecha no válida. Use el formato AAAA-MM-DD.") from None
    day = today()
    if end < start:
        raise invalid_dates("La fecha de fin no puede ser anterior a la de inicio.")
    if start < day - timedelta(days=PAST_TOLERANCE_DAYS):
        raise invalid_dates("La fecha de inicio ya pasó.")
    if (end - start).days + 1 > MAX_DAYS:
        raise invalid_dates(f"Un turno dura como máximo {MAX_DAYS} días.")
    if start > day + timedelta(days=MAX_DAYS_AHEAD):
        raise invalid_dates("Solo se reserva con hasta 24 meses de anticipación.")
    return start, end


def clean(value: str | None) -> str | None:
    return (value or "").strip() or None


def new_code(db: Session) -> str:
    for _ in range(8):
        code = "TUR-" + "".join(secrets.choice(TEMPORARY_ALPHABET) for _ in range(6))
        if db.scalar(select(Reservation.id).where(Reservation.code == code)) is None:
            return code
    raise AnalysisError(503, "code_unavailable", "No se pudo generar el código del turno. Inténtelo de nuevo.")


def create(db: Session, catalog: Catalog, user: User, payload: BookingRequest) -> ReservationResponse:
    place = find_place(catalog, payload.place_id)
    start, end = parse_range(payload.start_date, payload.end_date)
    permit = clean(payload.permit_reference)
    try:
        lock(db, f"agencia:{user.id}")
        lock(db, f"lugar:{place.id}")
        # Read under the lock: an override or a booking committed a moment ago is already visible.
        status = effective(place, db.get(PlaceOverride, place.id, populate_existing=True))
        if not status.bookable:
            raise AnalysisError(409, "place_blocked", f"{place.name} no se puede reservar. {status.reason}")
        if status.permit_required and permit is None:
            raise AnalysisError(422, "permit_required",
                                f"{place.name} está dentro de un área protegida: declare el número o la referencia "
                                "del permiso de su autoridad.")
        taken = list(db.scalars(select(Reservation).where(
            Reservation.place_id == place.id, Reservation.status == RESERVED,
            Reservation.start_date <= end, Reservation.end_date >= start).order_by(Reservation.start_date)))
        if taken:
            first = taken[0]
            who = ("Su agencia ya tiene este lugar" if first.user_id == user.id
                   else "Ocupado: otra agencia tiene este lugar")
            raise AnalysisError(409, "place_busy", f"{who} {spanish_range(first.start_date, first.end_date)}.",
                                {"busy": [{"start_date": row.start_date.isoformat(),
                                           "end_date": row.end_date.isoformat(),
                                           "mine": row.user_id == user.id} for row in taken]})
        active = db.scalar(select(func.count()).select_from(Reservation).where(
            Reservation.user_id == user.id, Reservation.status == RESERVED, Reservation.end_date >= today()))
        if active >= MAX_ACTIVE_PER_AGENCY:
            raise AnalysisError(409, "too_many_reservations",
                                f"Su agencia ya tiene {MAX_ACTIVE_PER_AGENCY} turnos activos. Cancele uno o espere a "
                                "que termine para reservar otro.")
        reservation = Reservation(
            code=new_code(db), user_id=user.id, place_id=place.id, place_name=place.name, start_date=start,
            end_date=end, status=RESERVED, permit_reference=permit if status.permit_required else None,
            mission=clean(payload.mission), robot=clean(payload.robot), people=payload.people,
            estimated_cost_usd=payload.estimated_cost_usd, created_at=utcnow())
        db.add(reservation)
        db.commit()
    except AnalysisError:
        db.rollback()  # releases the locks at once instead of when the request ends
        raise
    return reservation_response(reservation, user, today())


def cancel(db: Session, actor: User, reservation_id: str, reason: str | None) -> ReservationResponse:
    reservation = None
    if RESERVATION_ID.fullmatch(reservation_id):
        reservation = db.scalar(select(Reservation).where(Reservation.id == reservation_id).with_for_update()
                                .execution_options(populate_existing=True))
    # Someone else's turno looks exactly like one that does not exist.
    if reservation is None or (actor.role != ADMIN and reservation.user_id != actor.id):
        db.rollback()
        raise reservation_not_found()
    reason = clean(reason)
    try:
        if reservation.status == CANCELLED:
            raise AnalysisError(409, "already_cancelled", "Ese turno ya estaba cancelado.")
        if reservation.end_date < today() - timedelta(days=PAST_TOLERANCE_DAYS):
            raise AnalysisError(409, "reservation_finished", "Ese turno ya terminó: no se puede cancelar.")
        if actor.role == ADMIN and reason is None:
            raise AnalysisError(422, "reason_required", "Indique el motivo: la agencia lo verá en sus turnos.")
    except AnalysisError:
        db.rollback()
        raise
    reservation.status, reservation.cancelled_at = CANCELLED, utcnow()
    reservation.cancelled_by, reservation.cancel_reason = actor.role, reason
    db.commit()
    return reservation_response(reservation, db.get(User, reservation.user_id), today())


def set_override(db: Session, catalog: Catalog, actor: User, place_id: str, mode: str, reason: str) -> PlaceStatusResponse:
    place = find_place(catalog, place_id)
    reason = clean(reason)
    if reason is None:
        raise AnalysisError(422, "reason_required",
                            "Indique el motivo: las agencias lo verán al consultar el lugar.")
    if mode == ENABLED and place.base_status not in (PROTECTED, NO_DATA):
        raise AnalysisError(409, "not_blocked",
                            f"{place.name} no está bloqueado por un área protegida: no hace falta habilitarlo.")
    lock(db, f"lugar:{place.id}")
    override = db.get(PlaceOverride, place.id, populate_existing=True)
    if override is None:
        override = PlaceOverride(place_id=place.id)
        db.add(override)
    override.mode, override.reason, override.updated_at, override.updated_by = mode, reason, utcnow(), actor.id
    db.commit()
    return one_status(db, place)


def clear_override(db: Session, catalog: Catalog, place_id: str) -> PlaceStatusResponse:
    place = find_place(catalog, place_id)
    lock(db, f"lugar:{place.id}")
    override = db.get(PlaceOverride, place.id, populate_existing=True)
    if override is not None:
        db.delete(override)
    db.commit()
    return one_status(db, place)
