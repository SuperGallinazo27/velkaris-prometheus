"""Booking contracts (docs/TURNOS.md). Dates travel as AAAA-MM-DD text and are parsed by the
service, so a wrong date answers invalid_dates instead of the generic invalid_request."""

from typing import Annotated, Literal

from pydantic import Field

from api.account_schemas import AccountModel, bounded
from api.schemas import Finite, StrictModel

DateText = Annotated[str, Field(strict=True, pattern=r"^\d{4}-\d{2}-\d{2}$")]
PlaceId = Annotated[str, Field(strict=True, min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")]
PlaceStatus = Literal["libre", "permiso", "protegido", "sin_dato", "cerrado", "habilitado"]


class BookingRequest(StrictModel):
    place_id: PlaceId
    start_date: DateText
    end_date: DateText
    # Required when the place lies inside a protected area; the service reports its absence.
    permit_reference: bounded(200) | None = None
    mission: bounded(120) | None = None
    robot: bounded(80) | None = None
    people: Annotated[int, Field(strict=True, ge=1, le=500)] | None = None
    estimated_cost_usd: Annotated[Finite, Field(ge=0, le=1e10)] | None = None


class CancelRequest(StrictModel):
    reason: bounded(300) | None = None


class OverrideRequest(StrictModel):
    mode: Literal["habilitado", "cerrado"]
    reason: bounded(300)


class AreaResponse(AccountModel):
    site_id: int | None
    name: str
    name_eng: str | None
    designation: str | None
    designation_eng: str | None
    designation_type: str | None
    iucn: str | None
    status: str | None
    status_year: int | None
    governance: str | None
    authority: str | None
    iso3: str | None
    strict: bool


class OverrideResponse(AccountModel):
    mode: Literal["habilitado", "cerrado"]
    reason: str
    updated_at: str


class PlaceStatusResponse(AccountModel):
    id: str
    name: str
    province: str
    country: str
    status: PlaceStatus
    bookable: bool
    permit_required: bool
    reason: str
    # What the dataset alone says, before any decision of the administrator.
    dataset_status: Literal["libre", "permiso", "protegido", "sin_dato"]
    areas: list[AreaResponse]
    nearby_strict: list[AreaResponse]
    override: OverrideResponse | None
    occupied_today: bool
    active_reservations: int


class SourceResponse(AccountModel):
    name: str
    url: str
    terms: str
    citation: str
    release: str | None
    fetched: str | None


class Limits(AccountModel):
    max_days: int
    max_days_ahead: int
    max_active_per_agency: int


class PlacesResponse(AccountModel):
    source: SourceResponse
    strict_categories: list[str]
    limits: Limits
    today: str
    places: list[PlaceStatusResponse]


class BusyRange(AccountModel):
    start_date: str
    end_date: str
    mine: bool
    # Only the administrator sees who holds the range.
    agency: str | None


class AvailabilityResponse(AccountModel):
    place: PlaceStatusResponse
    busy: list[BusyRange]
    today: str
    limits: Limits


class AgencyInfo(AccountModel):
    user_id: str
    email: str
    agency: str
    acronym: str
    country: str


class ReservationResponse(AccountModel):
    id: str
    code: str
    place_id: str
    place_name: str
    start_date: str
    end_date: str
    days: int
    status: Literal["reservado", "cancelado"]
    phase: Literal["proximo", "en_curso", "terminado", "cancelado"]
    permit_reference: str | None
    mission: str | None
    robot: str | None
    people: int | None
    estimated_cost_usd: float | None
    created_at: str
    cancelled_at: str | None
    cancelled_by: Literal["admin", "agencia"] | None
    cancel_reason: str | None
    agency: AgencyInfo | None


class ReservationList(AccountModel):
    today: str
    reservations: list[ReservationResponse]
