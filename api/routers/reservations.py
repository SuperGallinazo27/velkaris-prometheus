"""Portal de turnos: place status, availability, reservations and the administrator's decisions.

Every route needs a session of an account that completed its registration. Agencies book and
cancel their own turnos; the administrator sees them all, cancels with a reason and closes or
enables places. Handlers are synchronous for the same reason as the account routes.
"""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request

from api.errors import AnalysisError
from api.models import ACTIVE, AGENCY, User
from api.reservation_schemas import (
    AvailabilityResponse, BookingRequest, CancelRequest, OverrideRequest, PlacesResponse, PlaceStatusResponse,
    ReservationList, ReservationResponse,
)
from api.routers.accounts import AccountRoute, Admin, Current, Database, errors, no_store
from api.services import places, reservations

router = APIRouter(prefix="/api/v1", route_class=AccountRoute, dependencies=[Depends(no_store)])

PLACE_ID_PATTERN = r"^[a-z0-9-]{1,64}$"
PlaceId = Annotated[str, Path(alias="place_id", pattern=PLACE_ID_PATTERN)]
ReservationId = Annotated[str, Path(alias="id", max_length=64)]


def active(current: Current) -> User:
    # A pending account has not registered its agency yet: it cannot see or book turnos.
    if current.user.status != ACTIVE:
        raise AnalysisError(403, "forbidden", "Complete su registro para usar el portal de turnos.")
    return current.user


Active = Annotated[User, Depends(active)]


def agency(user: Active) -> User:
    if user.role != AGENCY:
        raise AnalysisError(403, "forbidden", "Los turnos los reservan las agencias; el administrador los gestiona.")
    return user


Agency = Annotated[User, Depends(agency)]


def catalog(request: Request) -> places.Catalog:
    """Tests may inject their own catalog in app.state.places_catalog."""
    injected = getattr(request.app.state, "places_catalog", None)
    if injected is not None:
        return injected
    try:
        return places.default_catalog()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise AnalysisError(503, "places_unavailable",
                            "El catálogo de lugares no está disponible en este servidor.") from None


Places = Annotated[places.Catalog, Depends(catalog)]


@router.get("/places", response_model=PlacesResponse, tags=["turnos"], responses=errors(401, 403, 503))
def list_places(db: Database, _: Active, places_catalog: Places):
    return reservations.list_places(db, places_catalog)


@router.get("/places/{place_id}/availability", response_model=AvailabilityResponse, tags=["turnos"],
            responses=errors(401, 403, 404, 422, 503))
def place_availability(place_id: PlaceId, db: Database, user: Active, places_catalog: Places):
    return reservations.availability(db, places_catalog, place_id, user)


@router.post("/places/{place_id}/override", response_model=PlaceStatusResponse, tags=["turnos"],
             responses=errors(401, 403, 404, 409, 422, 503))
def set_place_override(place_id: PlaceId, payload: OverrideRequest, db: Database, actor: Admin, places_catalog: Places):
    return reservations.set_override(db, places_catalog, actor, place_id, payload.mode, payload.reason)


@router.delete("/places/{place_id}/override", response_model=PlaceStatusResponse, tags=["turnos"],
               responses=errors(401, 403, 404, 422, 503))
def clear_place_override(place_id: PlaceId, db: Database, _: Admin, places_catalog: Places):
    return reservations.clear_override(db, places_catalog, place_id)


@router.get("/reservations", response_model=ReservationList, tags=["turnos"], responses=errors(401, 403, 422, 503))
def list_reservations(db: Database, _: Admin,
                      status: Annotated[Literal["reservado", "cancelado"] | None, Query()] = None,
                      place_id: Annotated[str | None, Query(pattern=PLACE_ID_PATTERN)] = None):
    return reservations.list_all(db, status, place_id)


@router.get("/reservations/mine", response_model=ReservationList, tags=["turnos"], responses=errors(401, 403, 503))
def my_reservations(db: Database, user: Active):
    return reservations.list_mine(db, user)


@router.post("/reservations", response_model=ReservationResponse, status_code=201, tags=["turnos"],
             responses=errors(401, 403, 404, 409, 422, 503))
def create_reservation(payload: BookingRequest, db: Database, user: Agency, places_catalog: Places):
    return reservations.create(db, places_catalog, user, payload)


@router.post("/reservations/{id}/cancel", response_model=ReservationResponse, tags=["turnos"],
             responses=errors(401, 403, 404, 409, 422, 503))
def cancel_reservation(reservation_id: ReservationId, db: Database, user: Active,
                       payload: CancelRequest | None = None):
    return reservations.cancel(db, user, reservation_id, payload.reason if payload else None)
