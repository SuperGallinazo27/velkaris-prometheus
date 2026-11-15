import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api import security
from api.config import Settings
from api.db import build_engine
from api.main import create_app
from api.models import PlaceOverride, Reservation, utcnow
from api.services import places, reservations

# Every credential below is a TEST ONLY fixture.
ADMIN_EMAIL = "admin@example.org"
ADMIN_PASSWORD = "TEST-ONLY-admin-password"
OWN_PASSWORD = "clave-nueva-1"
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"
# Real rows of data/lugares.json (WDPCA, October 2026).
FREE_PLACE = "pampa-de-la-joya"
OTHER_FREE_PLACE = "yungay"
PERMIT_PLACE = "arenal-del-chimborazo"  # Chimborazo, Fauna Production Reserve, IUCN VI
STRICT_PLACE = "sierra-negra"  # Galápagos National Park, IUCN II
ROUTES = [("GET", "/api/v1/places"), ("GET", f"/api/v1/places/{FREE_PLACE}/availability"),
          ("POST", f"/api/v1/places/{FREE_PLACE}/override"), ("DELETE", f"/api/v1/places/{FREE_PLACE}/override"),
          ("GET", "/api/v1/reservations"), ("GET", "/api/v1/reservations/mine"), ("POST", "/api/v1/reservations"),
          ("POST", f"/api/v1/reservations/{UNKNOWN_ID}/cancel")]
ADMIN_ONLY = [("POST", f"/api/v1/places/{FREE_PLACE}/override"), ("DELETE", f"/api/v1/places/{FREE_PLACE}/override"),
              ("GET", "/api/v1/reservations")]


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    monkeypatch.setattr(security, "SCRYPT_LOG2_N", 4)
    monkeypatch.setattr(security, "DUMMY_HASH", security.hash_password("TEST ONLY"))


@pytest.fixture
def engine():
    instance = build_engine("sqlite://")
    yield instance
    instance.dispose()


@pytest.fixture
def client(engine):
    settings = Settings(environment="test", admin_email=ADMIN_EMAIL, admin_password=ADMIN_PASSWORD)
    with TestClient(create_app(settings, engine=engine)) as instance:
        yield instance


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def code(response):
    return response.json()["detail"]["code"]


def message(response):
    return response.json()["detail"]["message"]


def session_for(client, email, password):
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return bearer(response.json()["token"])


@pytest.fixture
def admin(client):
    return session_for(client, ADMIN_EMAIL, ADMIN_PASSWORD)


def pending_agency(client, admin, email):
    created = client.post("/api/v1/users", json={"email": email}, headers=admin)
    assert created.status_code == 201, created.text
    return created.json()["user"], session_for(client, email, created.json()["temporary_password"])


def new_agency(client, admin, name):
    """A registered agency: (user, session headers)."""
    email = f"{name}@example.org"
    user, pending = pending_agency(client, admin, email)
    profile = {"full_name": "Ana Pérez", "position": "", "agency": f"Agencia {name.upper()}", "acronym": name.upper(),
               "country": "Chile"}
    done = client.post("/api/v1/auth/registration", json={"password": OWN_PASSWORD, "profile": profile}, headers=pending)
    assert done.status_code == 204, done.text
    return user, session_for(client, email, OWN_PASSWORD)


@pytest.fixture
def agency(client, admin):
    return new_agency(client, admin, "uno")[1]


@pytest.fixture
def other(client, admin):
    return new_agency(client, admin, "dos")[1]


def day(offset):
    return (utcnow().date() + timedelta(days=offset)).isoformat()


def book(client, headers, place=FREE_PLACE, start=30, end=39, **extra):
    return client.post("/api/v1/reservations", headers=headers,
                       json={"place_id": place, "start_date": day(start), "end_date": day(end), **extra})


def booked(client, headers, **arguments):
    response = book(client, headers, **arguments)
    assert response.status_code == 201, response.text
    return response.json()


def status_of(client, headers, place):
    response = client.get(f"/api/v1/places/{place}/availability", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ---------- The dataset and the rule ----------

def test_real_catalog_classifies_the_37_places_with_the_iucn_rule():
    catalog = places.default_catalog()
    assert len(catalog.places) == 37
    assert "UNEP-WCMC and IUCN" in catalog.source["citation"] and catalog.source["release"]
    by_status = {}
    for place in catalog.places.values():
        by_status.setdefault(place.base_status, []).append(place.id)
    assert {status: len(ids) for status, ids in by_status.items()} == {"protegido": 11, "permiso": 7, "libre": 19}
    # The evaluators' example: Sierra Negra lies inside Galápagos National Park.
    sierra = catalog.places[STRICT_PLACE]
    assert sierra.base_status == "protegido"
    assert [(area.name, area.iucn) for area in sierra.deciding] == [("Galápagos", "II")]
    assert "Galápagos (Parque Nacional, categoría UICN II)" in places.dataset_reason(sierra)
    assert "protección estricta" in places.dataset_reason(sierra)
    chimborazo = catalog.places[PERMIT_PLACE]
    assert chimborazo.base_status == "permiso" and chimborazo.deciding[0].iucn == "VI"
    assert "declarando el permiso" in places.dataset_reason(chimborazo)
    assert catalog.places[FREE_PLACE].base_status == "libre" and catalog.places[FREE_PLACE].areas == ()
    # A strict area decides even when a milder one covers the same point.
    assert {area.iucn for area in catalog.places["craters-of-the-moon"].areas} == {"V", "Ib"}
    assert catalog.places["craters-of-the-moon"].base_status == "protegido"
    # A place just outside a national park stays bookable but carries the warning.
    valley = catalog.places["valle-de-la-muerte"]
    assert valley.base_status == "libre"
    assert {(area.name, area.iucn) for area in valley.nearby_strict} == {("Death Valley", "II"), ("Death Valley", "Ib")}
    # A name in another script is shown in English.
    assert catalog.places["crater-ramon"].deciding[0].display == "Har Ha-Negev (harhava 3)"
    # Designations read in Spanish: conventions are translated, national ones keep their wording
    # where it is Spanish and use the dataset's English elsewhere.
    assert places.dataset_reason(catalog.places["tolbachik"]).startswith(
        "Está dentro de Volcanoes of Kamchatka (Sitio del Patrimonio Mundial natural o mixto, sin categoría UICN informada).")
    assert "Vatnajökulsþjóðgarður (National Park, categoría UICN II)" in places.dataset_reason(catalog.places["askja"])
    assert "Chimborazo (Reserva de Producción de Fauna, categoría UICN VI)" in places.dataset_reason(chimborazo)


def test_rule_ignores_proposed_areas_and_blocks_places_without_data():
    area = {"siteId": 1, "name": "Parque", "designation": "Parque Nacional", "iucn": "II", "status": "Proposed"}
    catalog = places.parse_catalog({"source": {}, "places": [
        {"id": "propuesto", "name": "Propuesto", "latitude": 0, "longitude": 0, "protected": [area]},
        {"id": "vigente", "name": "Vigente", "latitude": 0, "longitude": 0, "protected": [{**area, "status": "Designated"}]},
        {"id": "sin-categoria", "name": "Sin categoría", "latitude": 0, "longitude": 0,
         "protected": [{**area, "iucn": "Not Reported", "status": "Designated"}]},
        {"id": "sin-consultar", "name": "Sin consultar", "latitude": 0, "longitude": 0, "protected": None},
    ]})
    assert [place.base_status for place in catalog.places.values()] == ["libre", "protegido", "permiso", "sin_dato"]
    assert "sin categoría UICN informada" in places.dataset_reason(catalog.places["sin-categoria"])
    assert "no se puede reservar" in places.dataset_reason(catalog.places["sin-consultar"])


def test_spanish_range_reads_naturally():
    assert reservations.spanish_range(date(2027, 3, 3), date(2027, 3, 12)) == "del 3 al 12 de marzo de 2027"
    assert reservations.spanish_range(date(2027, 2, 28), date(2027, 3, 4)) == "del 28 de febrero al 4 de marzo de 2027"
    assert reservations.spanish_range(date(2026, 12, 30), date(2027, 1, 2)) == "del 30 de diciembre de 2026 al 2 de enero de 2027"
    assert reservations.spanish_range(date(2027, 3, 3), date(2027, 3, 3)) == "el 3 de marzo de 2027"


# ---------- Permissions ----------

@pytest.mark.parametrize("method,path", ROUTES)
def test_every_route_needs_a_session(client, method, path):
    response = client.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 401 and code(response) == "not_authenticated"


@pytest.mark.parametrize("method,path", ROUTES)
def test_pending_accounts_cannot_use_the_portal(client, admin, method, path):
    _, pending = pending_agency(client, admin, "pendiente@example.org")
    response = client.request(method, path, headers=pending, json={} if method == "POST" else None)
    assert response.status_code == 403 and code(response) == "forbidden"


@pytest.mark.parametrize("method,path", ADMIN_ONLY)
def test_agencies_cannot_use_admin_routes(client, agency, method, path):
    body = {"mode": "cerrado", "reason": "x"} if method == "POST" else None
    response = client.request(method, path, headers=agency, json=body)
    assert response.status_code == 403 and code(response) == "forbidden"


def test_the_admin_manages_turnos_but_does_not_book(client, admin):
    response = book(client, admin)
    assert response.status_code == 403 and code(response) == "forbidden"
    assert client.get("/api/v1/reservations/mine", headers=admin).json()["reservations"] == []


# ---------- Places ----------

def test_places_list_carries_source_rule_and_status(client, agency):
    response = client.get("/api/v1/places", headers=agency)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["source"]["citation"].startswith("UNEP-WCMC and IUCN")
    assert body["source"]["url"] == "https://www.protectedplanet.net/" and body["source"]["release"]
    assert body["strict_categories"] == ["II", "III", "IV", "Ia", "Ib"]
    assert body["limits"] == {"max_days": 60, "max_days_ahead": 730, "max_active_per_agency": 5}
    assert body["today"] == day(0)
    rows = {row["id"]: row for row in body["places"]}
    assert len(rows) == 37
    strict = rows[STRICT_PLACE]
    assert (strict["status"], strict["bookable"], strict["permit_required"]) == ("protegido", False, False)
    assert strict["dataset_status"] == "protegido" and strict["override"] is None
    park = next(area for area in strict["areas"] if area["iucn"] == "II")
    assert park == {"site_id": 187, "name": "Galápagos", "name_eng": "Galápagos", "designation": "Parque Nacional",
                    "designation_eng": "National Park", "designation_type": "National", "iucn": "II",
                    "status": "Designated", "status_year": 1996,
                    "governance": "Federal or national ministry or agency", "authority": "Ministry of Environment",
                    "iso3": "ECU", "strict": True}
    assert "Galápagos" in strict["reason"]
    permit = rows[PERMIT_PLACE]
    assert (permit["status"], permit["bookable"], permit["permit_required"]) == ("permiso", True, True)
    free = rows[FREE_PLACE]
    assert (free["status"], free["bookable"], free["permit_required"]) == ("libre", True, False)
    assert free["areas"] == [] and free["occupied_today"] is False and free["active_reservations"] == 0
    assert [area["name"] for area in rows["valle-de-la-muerte"]["nearby_strict"]] == ["Death Valley", "Death Valley"]


def test_unknown_or_malformed_place(client, agency):
    missing = client.get("/api/v1/places/no-existe/availability", headers=agency)
    assert missing.status_code == 404 and code(missing) == "place_not_found"
    malformed = client.get("/api/v1/places/NO%20VALIDO/availability", headers=agency)
    assert malformed.status_code == 422 and code(malformed) == "invalid_request"
    assert book(client, agency, place="no-existe").status_code == 404


# ---------- Booking ----------

def test_booking_a_free_place_is_confirmed_at_once(client, admin, agency, other):
    created = book(client, agency, mission="Prueba de movilidad", robot="VIPER", people=4, estimated_cost_usd=18250)
    assert created.status_code == 201, created.text
    turno = created.json()
    assert re.fullmatch(r"TUR-[A-HJ-NP-Z2-9]{6}", turno["code"])
    assert turno == {**turno, "place_id": FREE_PLACE, "place_name": "Pampa de La Joya", "start_date": day(30),
                     "end_date": day(39), "days": 10, "status": "reservado", "phase": "proximo",
                     "permit_reference": None, "mission": "Prueba de movilidad", "robot": "VIPER", "people": 4,
                     "estimated_cost_usd": 18250.0, "cancelled_at": None, "cancelled_by": None, "cancel_reason": None}
    assert turno["agency"]["agency"] == "Agencia UNO" and turno["agency"]["email"] == "uno@example.org"

    mine = client.get("/api/v1/reservations/mine", headers=agency).json()
    assert mine["today"] == day(0) and [row["id"] for row in mine["reservations"]] == [turno["id"]]
    assert client.get("/api/v1/reservations/mine", headers=other).json()["reservations"] == []

    # The owner sees its range as its own; another agency sees it busy without knowing whose it is.
    own = status_of(client, agency, FREE_PLACE)
    assert own["busy"] == [{"start_date": day(30), "end_date": day(39), "mine": True, "agency": None}]
    assert own["place"]["active_reservations"] == 1 and own["place"]["occupied_today"] is False
    assert status_of(client, other, FREE_PLACE)["busy"] == [
        {"start_date": day(30), "end_date": day(39), "mine": False, "agency": None}]
    assert status_of(client, admin, FREE_PLACE)["busy"][0]["agency"] == "Agencia UNO"


def test_another_agency_finds_the_place_busy(client, agency, other):
    booked(client, agency, start=30, end=39)
    for start, end in [(30, 39), (25, 30), (39, 45), (32, 35), (20, 50)]:
        clash = book(client, other, start=start, end=end)
        assert clash.status_code == 409 and code(clash) == "place_busy", (start, end)
        assert message(clash).startswith("Ocupado: otra agencia tiene este lugar del ")
        assert clash.json()["detail"]["busy"] == [{"start_date": day(30), "end_date": day(39), "mine": False}]
    # The days right before and right after are free, and so is any other place.
    assert book(client, other, start=20, end=29).status_code == 201
    assert book(client, other, start=40, end=44).status_code == 201
    assert book(client, other, place=OTHER_FREE_PLACE, start=30, end=39).status_code == 201


def test_own_overlap_is_explained_as_such(client, agency):
    booked(client, agency)
    again = book(client, agency, start=35, end=36)
    assert again.status_code == 409 and code(again) == "place_busy"
    assert message(again).startswith("Su agencia ya tiene este lugar")
    assert again.json()["detail"]["busy"][0]["mine"] is True


def test_strictly_protected_place_cannot_be_booked_and_says_why(client, agency):
    blocked = book(client, agency, place=STRICT_PLACE, permit_reference="DPNG-2027-001")
    assert blocked.status_code == 409 and code(blocked) == "place_blocked"
    assert "Sierra Negra no se puede reservar" in message(blocked)
    assert "Galápagos (Parque Nacional, categoría UICN II)" in message(blocked)
    assert status_of(client, agency, STRICT_PLACE)["busy"] == []


def test_multiple_use_area_needs_the_declared_permit(client, admin, agency):
    missing = book(client, agency, place=PERMIT_PLACE)
    assert missing.status_code == 422 and code(missing) == "permit_required"
    blank = book(client, agency, place=PERMIT_PLACE, permit_reference="   ")
    assert blank.status_code == 422 and code(blank) == "permit_required"
    turno = booked(client, agency, place=PERMIT_PLACE, permit_reference="  MAATE-RPFCH-2027-014 ")
    assert turno["permit_reference"] == "MAATE-RPFCH-2027-014"
    listed = client.get("/api/v1/reservations", headers=admin).json()["reservations"]
    assert [row["permit_reference"] for row in listed] == ["MAATE-RPFCH-2027-014"]
    # A permit sent for a place that needs none is not stored.
    assert booked(client, agency, permit_reference="no hace falta")["permit_reference"] is None


@pytest.mark.parametrize("start,end,expected", [
    (10, 9, "La fecha de fin no puede ser anterior a la de inicio."),
    (-2, 3, "La fecha de inicio ya pasó."),
    (10, 70, "Un turno dura como máximo 60 días."),
    (731, 735, "Solo se reserva con hasta 24 meses de anticipación."),
])
def test_date_rules(client, agency, start, end, expected):
    response = book(client, agency, start=start, end=end)
    assert response.status_code == 422 and code(response) == "invalid_dates"
    assert message(response) == expected


def test_date_limits_are_inclusive(client, agency):
    assert book(client, agency, start=-1, end=0).status_code == 201  # a browser still on yesterday
    assert book(client, agency, place=OTHER_FREE_PLACE, start=10, end=69).status_code == 201  # exactly 60 days
    assert book(client, agency, place="dallol", start=730, end=730).status_code == 201


def test_malformed_bodies_are_rejected(client, agency):
    impossible = client.post("/api/v1/reservations", headers=agency,
                             json={"place_id": FREE_PLACE, "start_date": "2027-02-30", "end_date": "2027-03-02"})
    assert impossible.status_code == 422 and code(impossible) == "invalid_dates"
    for body in [{"place_id": FREE_PLACE, "start_date": "03/01/2027", "end_date": "2027-03-02"},
                 {"place_id": FREE_PLACE, "start_date": day(5)},
                 {"place_id": FREE_PLACE, "start_date": day(5), "end_date": day(6), "people": 0},
                 {"place_id": FREE_PLACE, "start_date": day(5), "end_date": day(6), "estimated_cost_usd": -1},
                 {"place_id": FREE_PLACE, "start_date": day(5), "end_date": day(6), "status": "reservado"},
                 {"place_id": FREE_PLACE, "start_date": day(5), "end_date": day(6), "mission": "x" * 121}]:
        response = client.post("/api/v1/reservations", headers=agency, json=body)
        assert response.status_code == 422 and code(response) == "invalid_request", body
    assert client.get("/api/v1/reservations/mine", headers=agency).json()["reservations"] == []


def test_an_agency_holds_at_most_five_active_turnos(client, agency):
    catalog = places.default_catalog()
    free = [place.id for place in catalog.places.values() if place.base_status == "libre"]
    turnos = [booked(client, agency, place=place) for place in free[:5]]
    sixth = book(client, agency, place=free[5])
    assert sixth.status_code == 409 and code(sixth) == "too_many_reservations"
    assert client.post(f"/api/v1/reservations/{turnos[0]['id']}/cancel", headers=agency).status_code == 200
    assert book(client, agency, place=free[5]).status_code == 201


# ---------- Cancelling ----------

def test_agency_cancels_its_own_turno_and_frees_the_dates(client, agency, other):
    turno = booked(client, agency)
    foreign = client.post(f"/api/v1/reservations/{turno['id']}/cancel", headers=other)
    assert foreign.status_code == 404 and code(foreign) == "reservation_not_found"
    cancelled = client.post(f"/api/v1/reservations/{turno['id']}/cancel", headers=agency, json={"reason": "Cambio de fechas"})
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert (body["status"], body["phase"], body["cancelled_by"], body["cancel_reason"]) == (
        "cancelado", "cancelado", "agencia", "Cambio de fechas")
    assert body["cancelled_at"] is not None
    again = client.post(f"/api/v1/reservations/{turno['id']}/cancel", headers=agency)
    assert again.status_code == 409 and code(again) == "already_cancelled"
    assert status_of(client, other, FREE_PLACE)["busy"] == []
    assert book(client, other).status_code == 201
    # The cancelled turno stays in the agency's history.
    assert [row["status"] for row in client.get("/api/v1/reservations/mine", headers=agency).json()["reservations"]] == ["cancelado"]


def test_admin_cancels_with_a_reason_the_agency_can_read(client, admin, agency):
    turno = booked(client, agency)
    silent = client.post(f"/api/v1/reservations/{turno['id']}/cancel", headers=admin)
    assert silent.status_code == 422 and code(silent) == "reason_required"
    blank = client.post(f"/api/v1/reservations/{turno['id']}/cancel", headers=admin, json={"reason": "  "})
    assert blank.status_code == 422 and code(blank) == "reason_required"
    done = client.post(f"/api/v1/reservations/{turno['id']}/cancel", headers=admin,
                       json={"reason": "El acceso por carretera está cortado"})
    assert done.status_code == 200, done.text
    seen = client.get("/api/v1/reservations/mine", headers=agency).json()["reservations"][0]
    assert (seen["status"], seen["cancelled_by"], seen["cancel_reason"]) == (
        "cancelado", "admin", "El acceso por carretera está cortado")


def test_unknown_reservation(client, agency, admin):
    for headers in (agency, admin):
        for reservation_id in (UNKNOWN_ID, "no-es-un-id"):
            response = client.post(f"/api/v1/reservations/{reservation_id}/cancel", headers=headers, json={"reason": "x"})
            assert response.status_code == 404 and code(response) == "reservation_not_found"


def test_phases_follow_the_calendar_and_finished_turnos_cannot_be_cancelled(client, agency, monkeypatch):
    turno = booked(client, agency, start=0, end=4)
    assert turno["phase"] == "en_curso"
    assert status_of(client, agency, FREE_PLACE)["place"]["occupied_today"] is True
    real_today = utcnow().date()
    monkeypatch.setattr(reservations, "today", lambda: real_today + timedelta(days=10))
    listed = client.get("/api/v1/reservations/mine", headers=agency).json()["reservations"][0]
    assert listed["phase"] == "terminado"
    late = client.post(f"/api/v1/reservations/{turno['id']}/cancel", headers=agency)
    assert late.status_code == 409 and code(late) == "reservation_finished"
    # A finished turno no longer counts as busy nor against the agency's limit.
    assert status_of(client, agency, FREE_PLACE)["busy"] == []
    assert status_of(client, agency, FREE_PLACE)["place"]["active_reservations"] == 0


# ---------- The administrator's decisions on a place ----------

def test_admin_closes_a_place_and_reopens_it(client, admin, agency):
    turno = booked(client, agency, start=30, end=34)
    closed = client.post(f"/api/v1/places/{FREE_PLACE}/override", headers=admin,
                         json={"mode": "cerrado", "reason": "Carretera cortada por lluvias."})
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert (body["status"], body["bookable"], body["dataset_status"]) == ("cerrado", False, "libre")
    assert body["reason"] == "Cerrado por el administrador: Carretera cortada por lluvias."
    assert body["override"]["mode"] == "cerrado" and body["active_reservations"] == 1

    seen = status_of(client, agency, FREE_PLACE)["place"]
    assert seen["status"] == "cerrado" and "Carretera cortada" in seen["reason"]
    blocked = book(client, agency, start=50, end=52)
    assert blocked.status_code == 409 and code(blocked) == "place_blocked"
    assert "Carretera cortada por lluvias." in message(blocked)
    # Turnos booked before the closure stay until someone cancels them.
    assert client.get("/api/v1/reservations/mine", headers=agency).json()["reservations"][0]["id"] == turno["id"]

    reopened = client.delete(f"/api/v1/places/{FREE_PLACE}/override", headers=admin)
    assert reopened.status_code == 200 and reopened.json()["status"] == "libre" and reopened.json()["override"] is None
    assert client.delete(f"/api/v1/places/{FREE_PLACE}/override", headers=admin).status_code == 200  # idempotent
    assert book(client, agency, start=50, end=52).status_code == 201


def test_admin_enables_a_protected_place_with_a_permit(client, admin, agency):
    needless = client.post(f"/api/v1/places/{FREE_PLACE}/override", headers=admin,
                           json={"mode": "habilitado", "reason": "x"})
    assert needless.status_code == 409 and code(needless) == "not_blocked"
    silent = client.post(f"/api/v1/places/{STRICT_PLACE}/override", headers=admin, json={"mode": "habilitado", "reason": " "})
    assert silent.status_code == 422 and code(silent) == "reason_required"

    enabled = client.post(f"/api/v1/places/{STRICT_PLACE}/override", headers=admin,
                          json={"mode": "habilitado", "reason": "Permiso de investigación DPNG PC-12-27."})
    assert enabled.status_code == 200, enabled.text
    body = enabled.json()
    assert (body["status"], body["bookable"], body["permit_required"], body["dataset_status"]) == (
        "habilitado", True, True, "protegido")
    assert "Galápagos" in body["reason"] and "DPNG PC-12-27" in body["reason"]

    missing = book(client, agency, place=STRICT_PLACE)
    assert missing.status_code == 422 and code(missing) == "permit_required"
    assert booked(client, agency, place=STRICT_PLACE, permit_reference="DPNG PC-12-27")["permit_reference"] == "DPNG PC-12-27"

    # Replacing the decision keeps one row per place; closing wins over the dataset too.
    replaced = client.post(f"/api/v1/places/{STRICT_PLACE}/override", headers=admin,
                           json={"mode": "cerrado", "reason": "Actividad volcánica."})
    assert replaced.json()["status"] == "cerrado"
    back = client.delete(f"/api/v1/places/{STRICT_PLACE}/override", headers=admin)
    assert back.json()["status"] == "protegido" and back.json()["bookable"] is False
    unknown = client.post("/api/v1/places/no-existe/override", headers=admin, json={"mode": "cerrado", "reason": "x"})
    assert unknown.status_code == 404 and code(unknown) == "place_not_found"


def test_one_override_row_per_place(client, admin, engine):
    for reason in ("Uno.", "Dos."):
        client.post(f"/api/v1/places/{FREE_PLACE}/override", headers=admin, json={"mode": "cerrado", "reason": reason})
    with Session(engine) as db:
        rows = list(db.scalars(select(PlaceOverride)))
    assert [(row.place_id, row.mode, row.reason) for row in rows] == [(FREE_PLACE, "cerrado", "Dos.")]


# ---------- The administrator's list ----------

def test_admin_lists_and_filters_every_turno(client, admin, agency, other):
    first = booked(client, agency, start=30, end=34)
    second = booked(client, other, place=OTHER_FREE_PLACE, start=20, end=22)
    client.post(f"/api/v1/reservations/{first['id']}/cancel", headers=agency)
    everything = client.get("/api/v1/reservations", headers=admin).json()
    assert [row["id"] for row in everything["reservations"]] == [second["id"], first["id"]]  # by start date
    assert {row["agency"]["agency"] for row in everything["reservations"]} == {"Agencia UNO", "Agencia DOS"}
    active = client.get("/api/v1/reservations", headers=admin, params={"status": "reservado"}).json()["reservations"]
    assert [row["id"] for row in active] == [second["id"]]
    by_place = client.get("/api/v1/reservations", headers=admin, params={"place_id": FREE_PLACE}).json()["reservations"]
    assert [row["id"] for row in by_place] == [first["id"]]
    invalid = client.get("/api/v1/reservations", headers=admin, params={"status": "otro"})
    assert invalid.status_code == 422 and code(invalid) == "invalid_request"


def test_deleting_an_agency_removes_its_turnos(client, admin, engine):
    user, session = new_agency(client, admin, "tres")
    booked(client, session)
    assert client.delete(f"/api/v1/users/{user['id']}", headers=admin).status_code == 204
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Reservation)) == 0
    assert status_of(client, admin, FREE_PLACE)["busy"] == []


# ---------- Concurrency ----------

def test_bookings_take_advisory_locks_on_postgresql_only():
    def session(dialect):
        calls = []
        fake = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name=dialect)),
                               execute=lambda statement, parameters: calls.append((str(statement), parameters)))
        return fake, calls

    postgres, calls = session("postgresql")
    reservations.lock(postgres, "lugar:yungay")
    assert calls == [("SELECT pg_advisory_xact_lock(hashtext(:key))", {"key": "lugar:yungay"})]
    sqlite, calls = session("sqlite")
    reservations.lock(sqlite, "lugar:yungay")
    assert calls == []
