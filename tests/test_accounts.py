import base64
import hashlib
import os
import re
import threading
import time
from datetime import datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api import manage, security
from api.config import Settings
from api.db import build_engine, normalize_database_url
from api.errors import AnalysisError
from api.main import create_app
from api.models import AuthSession, Base, User, utcnow
from api.services import accounts

# Captured at import, before the autouse fixture lowers the cost for this module.
PRODUCTION_COST = (security.SCRYPT_LOG2_N, security.SCRYPT_R, security.SCRYPT_P)
PRODUCTION_DUMMY_HASH = security.DUMMY_HASH

# Every credential below is a TEST ONLY fixture.
ADMIN_EMAIL = "admin@example.org"
ADMIN_PASSWORD = "TEST-ONLY-admin-password"
AGENCY_EMAIL = "agencia@example.org"
OWN_PASSWORD = "clave-nueva-1"
PROFILE = {"full_name": "Ana Pérez", "position": "Directora", "agency": "Agencia TEST", "acronym": "AT", "country": "Chile"}
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"
ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
LOGIN = ("POST", "/api/v1/auth/login")
SESSION_ROUTES = [("POST", "/api/v1/auth/logout"), ("GET", "/api/v1/auth/me"), ("POST", "/api/v1/auth/registration")]
ADMIN_ROUTES = [("GET", "/api/v1/users"), ("POST", "/api/v1/users"), ("GET", f"/api/v1/users/{UNKNOWN_ID}"),
                ("PATCH", f"/api/v1/users/{UNKNOWN_ID}"), ("DELETE", f"/api/v1/users/{UNKNOWN_ID}"),
                ("POST", f"/api/v1/users/{UNKNOWN_ID}/temporary-password")]


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    # The production parameters are exercised once, in test_production_scrypt_cost.
    monkeypatch.setattr(security, "SCRYPT_LOG2_N", 4)
    monkeypatch.setattr(security, "DUMMY_HASH", security.hash_password("TEST ONLY"))


@pytest.fixture
def engine():
    instance = build_engine("sqlite://")
    yield instance
    instance.dispose()


def settings(**overrides):
    return Settings(**{"environment": "test", "admin_email": ADMIN_EMAIL, "admin_password": ADMIN_PASSWORD, **overrides})


@pytest.fixture
def client(engine):
    with TestClient(create_app(settings(), engine=engine)) as instance:
        yield instance


@pytest.fixture
def in_flight(monkeypatch):
    """Parks one request inside a hashing call of the service, where production spends about a
    second outside any transaction, so that another request can complete in the meantime.
    Returns a function that lets the parked request finish and gives its response."""
    parked = []

    def start(hashing, request):
        real, entered, proceed, box, first = getattr(accounts, hashing), threading.Event(), threading.Event(), {}, [True]

        def hash_and_wait(*arguments):
            result = real(*arguments)
            if first and first.pop():  # later calls, from the overtaking request, go straight through
                entered.set()
                assert proceed.wait(30)
            return result

        monkeypatch.setattr(accounts, hashing, hash_and_wait)
        thread = threading.Thread(target=lambda: box.update(response=request()))
        thread.start()
        parked.append((thread, proceed))
        assert entered.wait(30), "the request never reached its hashing step"

        def finish():
            proceed.set()
            thread.join(30)
            return box["response"]

        return finish

    yield start
    for thread, proceed in parked:  # a failed assertion must not leave a request parked
        proceed.set()
        thread.join(30)


def code(response):
    return response.json()["detail"]["code"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def call(client, method, path, headers=None):
    return client.request(method, path, headers=headers, json={} if method in {"POST", "PATCH"} else None)


def login(client, email, password):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def session_for(client, email, password):
    response = login(client, email, password)
    assert response.status_code == 200, response.text
    return bearer(response.json()["token"])


@pytest.fixture
def admin(client):
    return session_for(client, ADMIN_EMAIL, ADMIN_PASSWORD)


def create_agency(client, admin, email=AGENCY_EMAIL, **body):
    response = client.post("/api/v1/users", json={"email": email, **body}, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()["user"], response.json()["temporary_password"]


def registered_agency(client, admin, email=AGENCY_EMAIL):
    user, temporary = create_agency(client, admin, email)
    pending = session_for(client, email, temporary)
    response = client.post("/api/v1/auth/registration", json={"password": OWN_PASSWORD, "profile": PROFILE}, headers=pending)
    assert response.status_code == 204, response.text
    return user, session_for(client, email, OWN_PASSWORD)


def moment(value):
    assert ISO_UTC.fullmatch(value), value
    return datetime.fromisoformat(value)


def test_full_onboarding_flow(client, admin):
    assert client.get("/health").json()["accounts_configured"] is True
    me = client.get("/api/v1/auth/me", headers=admin).json()
    assert me == {"id": me["id"], "email": ADMIN_EMAIL, "role": "admin", "status": "activo", "profile": None,
                  "created_at": me["created_at"], "registered_at": None, "temporary_expires_at": None}

    created = client.post("/api/v1/users", json={"email": "  Agencia@Example.ORG "}, headers=admin)
    assert created.status_code == 201, created.text
    user, temporary = created.json()["user"], created.json()["temporary_password"]
    assert set(created.json()) == {"user", "temporary_password"}
    assert user == {"id": user["id"], "email": AGENCY_EMAIL, "role": "agencia", "status": "pendiente", "profile": None,
                    "created_at": user["created_at"], "registered_at": None,
                    "temporary_expires_at": user["temporary_expires_at"]}
    assert moment(user["temporary_expires_at"]) - moment(user["created_at"]) == timedelta(days=7)

    first = login(client, AGENCY_EMAIL, temporary)
    assert first.status_code == 200, first.text
    assert set(first.json()) == {"token", "expires_at", "user"}
    assert first.json()["user"] == user
    assert timedelta(hours=11, minutes=59) < moment(first.json()["expires_at"]) - utcnow() <= timedelta(hours=12)
    pending = bearer(first.json()["token"])
    other_pending = session_for(client, AGENCY_EMAIL, temporary)
    assert client.get("/api/v1/auth/me", headers=pending).json() == user
    blocked = client.get("/api/v1/users", headers=pending)
    assert blocked.status_code == 403 and code(blocked) == "forbidden"

    registration = client.post("/api/v1/auth/registration", json={"password": OWN_PASSWORD, "profile": PROFILE}, headers=pending)
    assert registration.status_code == 204 and registration.content == b""
    for old in (pending, other_pending):  # every session of that user is gone
        assert code(client.get("/api/v1/auth/me", headers=old)) == "not_authenticated"
    stale = login(client, AGENCY_EMAIL, temporary)
    assert stale.status_code == 401 and code(stale) == "invalid_credentials"

    active = login(client, AGENCY_EMAIL, OWN_PASSWORD).json()
    assert active["user"] == {**user, "status": "activo", "profile": PROFILE, "temporary_expires_at": None,
                              "registered_at": active["user"]["registered_at"]}
    assert moment(active["user"]["registered_at"]) >= moment(user["created_at"])
    again = client.post("/api/v1/auth/registration", json={"password": "otra-clave-2", "profile": PROFILE},
                        headers=bearer(active["token"]))
    assert again.status_code == 409 and code(again) == "not_pending"
    listing = client.get("/api/v1/users", headers=admin).json()
    assert [row["email"] for row in listing["users"]] == [ADMIN_EMAIL, AGENCY_EMAIL]
    assert listing["users"][1] == active["user"]


def test_users_are_listed_by_creation(client, admin):
    emails = [f"agencia-{index}@example.org" for index in (3, 1, 2)]
    for email in emails:
        create_agency(client, admin, email)
    assert [row["email"] for row in client.get("/api/v1/users", headers=admin).json()["users"]] == [ADMIN_EMAIL, *emails]


def test_wrong_password_and_unknown_email_are_indistinguishable(client, engine):
    wrong = login(client, ADMIN_EMAIL, "TEST-ONLY-incorrecta")
    unknown = login(client, "nadie@example.org", ADMIN_PASSWORD)
    malformed = login(client, "no es un correo", ADMIN_PASSWORD)
    assert wrong.status_code == unknown.status_code == malformed.status_code == 401
    assert wrong.json() == unknown.json() == malformed.json()
    assert code(wrong) == "invalid_credentials"
    assert wrong.headers["www-authenticate"] == "Bearer"
    with Session(engine) as db:  # a corrupt stored hash is a failed login, not a crash
        db.execute(update(User).values(password_hash="corrupto"))
        db.commit()
    assert login(client, ADMIN_EMAIL, ADMIN_PASSWORD).json() == wrong.json()


def test_login_normalizes_the_email(client):
    assert login(client, "  ADMIN@Example.org ", ADMIN_PASSWORD).status_code == 200
    assert code(login(client, ADMIN_EMAIL, f" {ADMIN_PASSWORD} ")) == "invalid_credentials"  # passwords are exact


@pytest.mark.parametrize("body", [
    {}, {"email": ADMIN_EMAIL}, {"password": ADMIN_PASSWORD}, {"email": ADMIN_EMAIL, "password": None},
    {"email": 1, "password": ADMIN_PASSWORD}, {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "remember": True},
    [ADMIN_EMAIL, ADMIN_PASSWORD],
])
def test_malformed_login_body(client, body):
    response = client.post("/api/v1/auth/login", json=body)
    assert response.status_code == 422 and code(response) == "invalid_request"
    assert ADMIN_PASSWORD not in response.text


def test_unencodable_text_is_rejected_not_crashed(client, admin):
    # json.loads accepts lone surrogates; they can be neither hashed nor stored.
    surrogate = b'{"email":"admin@example.org","password":"\\ud800TEST-ONLY"}'
    response = client.post("/api/v1/auth/login", content=surrogate, headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and code(response) == "invalid_request"
    assert code(login(client, "a\u0000@example.org", "TEST-ONLY-clave")) == "invalid_credentials"
    assert code(client.post("/api/v1/users", json={"email": "a\u0000@example.org"}, headers=admin)) == "invalid_email"


def test_expired_temporary_password(client, engine, admin):
    user, temporary = create_agency(client, admin)
    with Session(engine) as db:
        db.execute(update(User).where(User.id == user["id"]).values(temporary_expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    for _ in range(6):  # a correct but expired password is not a failed attempt
        response = login(client, AGENCY_EMAIL, temporary)
        assert response.status_code == 401 and code(response) == "temporary_password_expired"
    # A wrong password does not reveal that the account exists or expired.
    assert code(login(client, AGENCY_EMAIL, "TEST-ONLY-incorrecta")) == "invalid_credentials"
    renewed = client.post(f"/api/v1/users/{user['id']}/temporary-password", headers=admin).json()["temporary_password"]
    assert login(client, AGENCY_EMAIL, renewed).status_code == 200


def test_pending_session_never_outlives_the_temporary_password(engine):
    with TestClient(create_app(settings(temporary_password_days=0.25), engine=engine)) as client:
        user, temporary = create_agency(client, session_for(client, ADMIN_EMAIL, ADMIN_PASSWORD))
        response = login(client, AGENCY_EMAIL, temporary).json()
        assert moment(user["temporary_expires_at"]) - moment(user["created_at"]) == timedelta(hours=6)
        assert response["expires_at"] == user["temporary_expires_at"]


def test_expired_session(engine):
    with TestClient(create_app(settings(session_hours=2), engine=engine)) as client:
        response = login(client, ADMIN_EMAIL, ADMIN_PASSWORD).json()
        assert timedelta(hours=1, minutes=59) < moment(response["expires_at"]) - utcnow() <= timedelta(hours=2)
        headers = bearer(response["token"])
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
        with Session(engine) as db:
            db.execute(update(AuthSession).values(expires_at=utcnow() - timedelta(seconds=1)))
            db.commit()
        for method, path in SESSION_ROUTES + ADMIN_ROUTES:
            expired = call(client, method, path, headers)
            assert expired.status_code == 401 and code(expired) == "session_expired", (method, path)
        # The next login purges expired sessions.
        session_for(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert code(client.get("/api/v1/auth/me", headers=headers)) == "not_authenticated"
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(AuthSession)) == 1


def test_logout_invalidates_only_that_token(client, admin):
    other = session_for(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    response = client.post("/api/v1/auth/logout", headers=admin)
    assert response.status_code == 204 and response.content == b""
    for method, path in SESSION_ROUTES + ADMIN_ROUTES:
        assert code(call(client, method, path, admin)) == "not_authenticated"
    assert client.get("/api/v1/auth/me", headers=other).status_code == 200


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": ""}, {"Authorization": "Bearer"}, {"Authorization": "Bearer "},
    {"Authorization": "Basic YWRtaW46YWRtaW4="}, {"Authorization": "Token abc"},
    {"Authorization": "Bearer desconocido"}, {"Authorization": "Bearer " + "x" * 500},
])
def test_missing_malformed_or_unknown_token(client, admin, headers):
    for method, path in SESSION_ROUTES + ADMIN_ROUTES:
        response = call(client, method, path, headers)
        assert response.status_code == 401 and code(response) == "not_authenticated", (method, path)
        assert response.headers["www-authenticate"] == "Bearer"


def test_bearer_scheme_is_case_insensitive(client, admin):
    token = admin["Authorization"].removeprefix("Bearer ")
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"bearer {token}"}).status_code == 200


def test_agency_cannot_administer(client, admin):
    user, agency = registered_agency(client, admin)
    for method, path in ADMIN_ROUTES:
        for target in (path, path.replace(UNKNOWN_ID, user["id"])):
            response = call(client, method, target, agency)
            assert response.status_code == 403 and code(response) == "forbidden", (method, target)
    assert client.get("/api/v1/auth/me", headers=agency).json()["status"] == "activo"  # nothing was changed
    assert len(client.get("/api/v1/users", headers=admin).json()["users"]) == 2


def test_pending_user_limits(client, admin):
    user, temporary = create_agency(client, admin)
    pending = session_for(client, AGENCY_EMAIL, temporary)
    for method, path in ADMIN_ROUTES:
        response = call(client, method, path.replace(UNKNOWN_ID, user["id"]), pending)
        assert response.status_code == 403 and code(response) == "forbidden", (method, path)
    assert client.get("/api/v1/auth/me", headers=pending).json()["status"] == "pendiente"
    assert client.post("/api/v1/auth/logout", headers=pending).status_code == 204
    assert code(client.get("/api/v1/auth/me", headers=pending)) == "not_authenticated"
    assert client.post("/api/v1/analyze", json={}).status_code == 200  # analysis stays public


@pytest.mark.parametrize("body,expected", [
    ({"password": "corta-1", "profile": PROFILE}, "password_too_short"),
    ({"password": "x" * 129, "profile": PROFILE}, "password_too_long"),
    ({"password": OWN_PASSWORD, "profile": {**PROFILE, "full_name": "   "}}, "incomplete_profile"),
    ({"password": OWN_PASSWORD, "profile": {**PROFILE, "agency": ""}}, "incomplete_profile"),
    ({"password": OWN_PASSWORD, "profile": {**PROFILE, "country": None}}, "incomplete_profile"),
    ({"password": OWN_PASSWORD, "profile": {"full_name": "Ana Pérez", "agency": "Agencia TEST"}}, "incomplete_profile"),
    ({"password": OWN_PASSWORD, "profile": {}}, "incomplete_profile"),
    ({"password": OWN_PASSWORD}, "invalid_request"),
    ({"profile": PROFILE}, "invalid_request"),
    ({"password": 12345678, "profile": PROFILE}, "invalid_request"),
    ({"password": OWN_PASSWORD, "profile": PROFILE, "role": "admin"}, "invalid_request"),
    ({"password": OWN_PASSWORD, "profile": {**PROFILE, "status": "activo"}}, "invalid_request"),
    ({"password": OWN_PASSWORD, "profile": {**PROFILE, "full_name": "x" * 121}}, "invalid_request"),
    ({"password": OWN_PASSWORD, "profile": {**PROFILE, "position": "x" * 81}}, "invalid_request"),
    ({"password": OWN_PASSWORD, "profile": {**PROFILE, "acronym": "x" * 21}}, "invalid_request"),
    ({"password": OWN_PASSWORD, "profile": {**PROFILE, "country": "Chi\u0000le"}}, "invalid_request"),
])
def test_registration_validation(client, admin, body, expected):
    _, temporary = create_agency(client, admin)
    pending = session_for(client, AGENCY_EMAIL, temporary)
    response = client.post("/api/v1/auth/registration", json=body, headers=pending)
    assert response.status_code == 422 and code(response) == expected
    assert response.json()["detail"]["message"]
    me = client.get("/api/v1/auth/me", headers=pending).json()  # nothing changed, the session survives
    assert me["status"] == "pendiente" and me["profile"] is None


def test_new_password_must_differ_from_the_temporary_one(client, admin):
    _, temporary = create_agency(client, admin)
    pending = session_for(client, AGENCY_EMAIL, temporary)
    response = client.post("/api/v1/auth/registration", json={"password": temporary, "profile": PROFILE}, headers=pending)
    assert response.status_code == 422 and code(response) == "password_same_as_temporary"
    assert login(client, AGENCY_EMAIL, temporary).status_code == 200


@pytest.mark.parametrize("length", [8, 128])
def test_password_length_bounds_are_inclusive(client, admin, length):
    _, temporary = create_agency(client, admin)
    pending = session_for(client, AGENCY_EMAIL, temporary)
    profile = {"full_name": " Ana Pérez ", "agency": "Agencia TEST", "country": "Chile"}  # optional fields omitted
    password = "ñ" * length
    assert client.post("/api/v1/auth/registration", json={"password": password, "profile": profile}, headers=pending).status_code == 204
    me = client.get("/api/v1/auth/me", headers=session_for(client, AGENCY_EMAIL, password)).json()
    assert me["profile"] == {"full_name": "Ana Pérez", "position": "", "agency": "Agencia TEST", "acronym": "", "country": "Chile"}


@pytest.mark.parametrize("email", [
    "", "   ", "sin-arroba.example.org", "dos@@example.org", "a@b@example.org", "a@sin-punto", "a b@example.org",
    "a@exa mple.org", "a@.org", "a@example.", "a@example..org", "@example.org", "x" * 243 + "@example.org",
    "a@example.org\nb@example.org", "a\u0000@example.org",
])
def test_invalid_email(client, admin, email):
    response = client.post("/api/v1/users", json={"email": email}, headers=admin)
    assert response.status_code == 422 and code(response) == "invalid_email"
    assert len(client.get("/api/v1/users", headers=admin).json()["users"]) == 1


def test_longest_valid_email(client, admin):
    email = "x" * 242 + "@example.org"
    assert len(email) == 254
    assert create_agency(client, admin, email)[0]["email"] == email


def test_duplicate_email_is_case_insensitive(client, admin):
    create_agency(client, admin, "Agencia@Example.org")
    for email in (AGENCY_EMAIL, " AGENCIA@EXAMPLE.ORG ", ADMIN_EMAIL.upper()):
        response = client.post("/api/v1/users", json={"email": email}, headers=admin)
        assert response.status_code == 409 and code(response) == "email_exists"
    assert len(client.get("/api/v1/users", headers=admin).json()["users"]) == 2


@pytest.mark.parametrize("body,status,expected", [
    ({"email": AGENCY_EMAIL, "temporary_password": "corta-1"}, 422, "password_too_short"),
    ({"email": AGENCY_EMAIL, "temporary_password": ""}, 422, "password_too_short"),
    ({"email": AGENCY_EMAIL, "temporary_password": "x" * 129}, 422, "password_too_long"),
    ({}, 422, "invalid_request"),
    ({"temporary_password": "temporal-elegida"}, 422, "invalid_request"),
    ({"email": 7}, 422, "invalid_request"),
    ({"email": AGENCY_EMAIL, "temporary_password": 12345678}, 422, "invalid_request"),
    ({"email": AGENCY_EMAIL, "role": "admin"}, 422, "invalid_request"),
    ({"email": AGENCY_EMAIL, "status": "activo"}, 422, "invalid_request"),
])
def test_create_user_validation(client, admin, body, status, expected):
    response = client.post("/api/v1/users", json=body, headers=admin)
    assert response.status_code == status and code(response) == expected
    assert len(client.get("/api/v1/users", headers=admin).json()["users"]) == 1


def test_supplied_temporary_password_is_kept_verbatim(client, admin):
    user, temporary = create_agency(client, admin, temporary_password=" temporal elegida ")
    assert temporary == " temporal elegida "
    assert code(login(client, AGENCY_EMAIL, "temporal elegida")) == "invalid_credentials"
    assert login(client, AGENCY_EMAIL, temporary).json()["user"] == user
    assert create_agency(client, admin, "otra@example.org", temporary_password=None)[1] != temporary


def test_generated_temporary_passwords(client, admin):
    values = {security.generate_temporary_password() for _ in range(200)}
    values.add(create_agency(client, admin)[1])
    assert len(values) == 201
    for value in values:
        assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}(-[A-HJ-NP-Z2-9]{4}){2}", value), value
    assert not set(security.TEMPORARY_ALPHABET) & set("0O1lI")


def test_get_and_patch_user(client, admin):
    user, agency = registered_agency(client, admin)
    path = f"/api/v1/users/{user['id']}"
    current = client.get(path, headers=admin)
    assert current.status_code == 200 and current.json() == client.get("/api/v1/auth/me", headers=agency).json()

    renamed = client.patch(path, json={"email": " Nueva@Example.org "}, headers=admin)
    assert renamed.status_code == 200 and renamed.json() == {**current.json(), "email": "nueva@example.org"}
    assert client.get("/api/v1/auth/me", headers=agency).json()["email"] == "nueva@example.org"  # session kept
    assert code(login(client, AGENCY_EMAIL, OWN_PASSWORD)) == "invalid_credentials"
    assert login(client, "nueva@example.org", OWN_PASSWORD).status_code == 200

    profile = {"full_name": " Luis Soto ", "agency": "Otra agencia", "country": "Perú", "acronym": ""}
    edited = client.patch(path, json={"profile": profile}, headers=admin)
    assert edited.status_code == 200 and edited.json() == {**renamed.json(), "profile": {
        "full_name": "Luis Soto", "position": "", "agency": "Otra agencia", "acronym": "", "country": "Perú"}}

    both = client.patch(path, json={"email": AGENCY_EMAIL, "profile": PROFILE}, headers=admin)
    assert both.json() == current.json()
    assert client.patch(path, json={"email": AGENCY_EMAIL.upper()}, headers=admin).json() == current.json()  # no-op
    assert client.get(path, headers=admin).json() == current.json()


def test_admin_profile_and_pending_profile_can_be_edited(client, admin):
    me = client.get("/api/v1/auth/me", headers=admin).json()
    edited = client.patch(f"/api/v1/users/{me['id']}", json={"profile": PROFILE}, headers=admin)
    assert edited.json() == {**me, "profile": PROFILE}
    assert client.get("/api/v1/auth/me", headers=admin).json() == edited.json()
    user, _ = create_agency(client, admin)
    assert client.patch(f"/api/v1/users/{user['id']}", json={"profile": PROFILE}, headers=admin).json() == {**user, "profile": PROFILE}


@pytest.mark.parametrize("body,status,expected", [
    ({}, 422, "invalid_request"),
    ({"email": None, "profile": None}, 422, "invalid_request"),
    ({"role": "admin"}, 422, "invalid_request"),
    ({"email": AGENCY_EMAIL, "status": "activo"}, 422, "invalid_request"),
    ({"password": OWN_PASSWORD}, 422, "invalid_request"),
    ({"profile": {**PROFILE, "full_name": "x" * 121}}, 422, "invalid_request"),
    ({"email": "sin-arroba"}, 422, "invalid_email"),
    ({"email": ""}, 422, "invalid_email"),
    ({"profile": {"full_name": "Ana Pérez"}}, 422, "incomplete_profile"),
    ({"email": "otra@example.org", "profile": {**PROFILE, "country": " "}}, 422, "incomplete_profile"),
    ({"email": ADMIN_EMAIL.upper()}, 409, "email_exists"),
])
def test_patch_validation(client, admin, body, status, expected):
    user, _ = registered_agency(client, admin)
    path = f"/api/v1/users/{user['id']}"
    before = client.get(path, headers=admin).json()
    response = client.patch(path, json=body, headers=admin)
    assert response.status_code == status and code(response) == expected
    assert client.get(path, headers=admin).json() == before


@pytest.mark.parametrize("user_id", [UNKNOWN_ID, "1", "no-es-un-uuid", UNKNOWN_ID.upper(), "%00", "' OR 1=1 --"])
def test_user_not_found(client, admin, user_id):
    for method, path in ADMIN_ROUTES[2:]:
        body = {"email": "otra@example.org"} if method == "PATCH" else None
        response = client.request(method, path.replace(UNKNOWN_ID, user_id), headers=admin, json=body)
        assert response.status_code == 404 and code(response) == "user_not_found", (method, path)


def test_delete_rules(client, engine, admin):
    me = client.get("/api/v1/auth/me", headers=admin).json()
    own = client.delete(f"/api/v1/users/{me['id']}", headers=admin)
    assert own.status_code == 409 and code(own) == "cannot_delete_self"

    user, agency = registered_agency(client, admin)
    other_session = session_for(client, AGENCY_EMAIL, OWN_PASSWORD)
    deleted = client.delete(f"/api/v1/users/{user['id']}", headers=admin)
    assert deleted.status_code == 204 and deleted.content == b""
    for headers in (agency, other_session):
        assert code(client.get("/api/v1/auth/me", headers=headers)) == "not_authenticated"
    with Session(engine) as db:
        assert db.scalars(select(AuthSession.user_id)).all() == [me["id"]]
    assert code(client.delete(f"/api/v1/users/{user['id']}", headers=admin)) == "user_not_found"
    assert code(login(client, AGENCY_EMAIL, OWN_PASSWORD)) == "invalid_credentials"
    assert [row["id"] for row in client.get("/api/v1/users", headers=admin).json()["users"]] == [me["id"]]
    assert create_agency(client, admin)[0]["id"] != user["id"]  # the address is free again


def test_sessions_cascade_in_the_database(client, engine, admin):
    user, _ = registered_agency(client, admin)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user["id"])) == 1
        db.execute(delete(User).where(User.id == user["id"]))  # no application code involved
        db.commit()
        assert db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user["id"])) == 0


def test_last_admin_is_never_deleted(client, engine, admin):
    first = client.get("/api/v1/auth/me", headers=admin).json()["id"]
    with Session(engine) as db:
        # Over HTTP the caller is an admin too, so this guard can only fire in a race: call it directly.
        with pytest.raises(AnalysisError) as error:
            accounts.delete_user(db, User(id=UNKNOWN_ID), first)
        assert (error.value.status_code, error.value.code) == (409, "last_admin")
        assert accounts.ensure_admin(db, "segundo@example.org", "TEST-ONLY-segundo") == "created"
    second_session = session_for(client, "segundo@example.org", "TEST-ONLY-segundo")
    second = client.get("/api/v1/auth/me", headers=second_session).json()["id"]
    assert code(client.delete(f"/api/v1/users/{second}", headers=second_session)) == "cannot_delete_self"
    assert client.delete(f"/api/v1/users/{first}", headers=second_session).status_code == 204
    assert code(client.get("/api/v1/auth/me", headers=admin)) == "not_authenticated"
    assert code(client.delete(f"/api/v1/users/{second}", headers=second_session)) == "cannot_delete_self"
    assert [row["id"] for row in client.get("/api/v1/users", headers=second_session).json()["users"]] == [second]


def test_temporary_password_reset_on_active_agency(client, admin):
    user, agency = registered_agency(client, admin)
    path = f"/api/v1/users/{user['id']}/temporary-password"
    before = client.get("/api/v1/auth/me", headers=agency).json()
    response = client.post(path, headers=admin)  # the body is optional
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"user", "temporary_password"}
    reset, temporary = response.json()["user"], response.json()["temporary_password"]
    assert reset == {**before, "status": "pendiente", "temporary_expires_at": reset["temporary_expires_at"]}
    assert reset["profile"] == PROFILE and reset["registered_at"] == before["registered_at"]
    assert timedelta(days=6, hours=23) < moment(reset["temporary_expires_at"]) - utcnow() <= timedelta(days=7)

    assert code(client.get("/api/v1/auth/me", headers=agency)) == "not_authenticated"
    assert code(login(client, AGENCY_EMAIL, OWN_PASSWORD)) == "invalid_credentials"
    pending = session_for(client, AGENCY_EMAIL, temporary)
    assert client.get("/api/v1/auth/me", headers=pending).json() == reset
    assert code(client.get("/api/v1/users", headers=pending)) == "forbidden"
    assert client.post("/api/v1/auth/registration", json={"password": "clave-nueva-2", "profile": PROFILE}, headers=pending).status_code == 204
    assert login(client, AGENCY_EMAIL, "clave-nueva-2").json()["user"]["status"] == "activo"

    chosen = client.post(path, json={"temporary_password": "temporal-elegida"}, headers=admin)
    assert chosen.status_code == 200 and chosen.json()["temporary_password"] == "temporal-elegida"
    assert login(client, AGENCY_EMAIL, "temporal-elegida").json()["user"]["status"] == "pendiente"
    assert client.post(path, json={}, headers=admin).json()["temporary_password"] != "temporal-elegida"
    assert client.post(path, json={"temporary_password": None}, headers=admin).status_code == 200


@pytest.mark.parametrize("body,expected", [
    ({"temporary_password": "corta-1"}, "password_too_short"),
    ({"temporary_password": "x" * 129}, "password_too_long"),
    ({"temporary_password": 12345678}, "invalid_request"),
    ({"password": "temporal-elegida"}, "invalid_request"),
])
def test_temporary_password_reset_validation(client, admin, body, expected):
    user, agency = registered_agency(client, admin)
    response = client.post(f"/api/v1/users/{user['id']}/temporary-password", json=body, headers=admin)
    assert response.status_code == 422 and code(response) == expected
    assert client.get("/api/v1/auth/me", headers=agency).json()["status"] == "activo"


def test_temporary_password_is_not_allowed_for_admins(client, admin):
    me = client.get("/api/v1/auth/me", headers=admin).json()
    response = client.post(f"/api/v1/users/{me['id']}/temporary-password", headers=admin)
    assert response.status_code == 409 and code(response) == "not_allowed_for_admin"
    assert client.get("/api/v1/auth/me", headers=admin).json() == me
    assert login(client, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200


def test_login_throttling(client):
    now = [1000.0]
    client.app.state.login_throttle.clock = lambda: now[0]
    for attempt in range(5):
        response = login(client, ADMIN_EMAIL if attempt % 2 else " ADMIN@Example.org ", "TEST-ONLY-incorrecta")
        assert response.status_code == 401 and code(response) == "invalid_credentials"
    for password in (ADMIN_PASSWORD, "TEST-ONLY-incorrecta"):  # even the right password waits
        blocked = login(client, ADMIN_EMAIL, password)
        assert blocked.status_code == 429 and code(blocked) == "too_many_attempts"
    assert code(login(client, "otra@example.org", "TEST-ONLY-incorrecta")) == "invalid_credentials"  # per email
    now[0] += 899
    assert login(client, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 429
    now[0] += 2  # the window passed
    assert login(client, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200


def test_successful_login_clears_failed_attempts(client, engine):
    for _ in range(3):
        for _ in range(4):
            assert code(login(client, ADMIN_EMAIL, "TEST-ONLY-incorrecta")) == "invalid_credentials"
        assert login(client, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200
    for _ in range(5):
        login(client, ADMIN_EMAIL, "TEST-ONLY-incorrecta")
    assert login(client, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 429
    with TestClient(create_app(settings(), engine=engine)) as other:  # each app instance has its own counters
        assert login(other, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200


def sessions_of(engine, user):
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user["id"]))


def test_login_in_flight_does_not_survive_a_reset(client, engine, admin, in_flight):
    user, agency = registered_agency(client, admin)
    finish = in_flight("verify_password", lambda: login(client, AGENCY_EMAIL, OWN_PASSWORD))
    reset = client.post(f"/api/v1/users/{user['id']}/temporary-password", headers=admin)
    assert reset.status_code == 200 and sessions_of(engine, user) == 0
    late = finish()  # its password was right when it was read, but the row changed while hashing
    assert late.status_code == 401 and code(late) == "invalid_credentials"
    assert sessions_of(engine, user) == 0
    assert code(client.get("/api/v1/auth/me", headers=agency)) == "not_authenticated"
    assert login(client, AGENCY_EMAIL, reset.json()["temporary_password"]).json()["user"]["status"] == "pendiente"


def test_login_in_flight_does_not_survive_registration_or_deletion(client, engine, admin, in_flight):
    user, temporary = create_agency(client, admin)
    pending = session_for(client, AGENCY_EMAIL, temporary)
    finish = in_flight("verify_password", lambda: login(client, AGENCY_EMAIL, temporary))
    body = {"password": OWN_PASSWORD, "profile": PROFILE}
    assert client.post("/api/v1/auth/registration", json=body, headers=pending).status_code == 204
    late = finish()
    assert late.status_code == 401 and code(late) == "invalid_credentials"
    assert sessions_of(engine, user) == 0

    finish = in_flight("verify_password", lambda: login(client, AGENCY_EMAIL, OWN_PASSWORD))
    assert client.delete(f"/api/v1/users/{user['id']}", headers=admin).status_code == 204
    late = finish()  # the account is gone: a failed login, not a database error
    assert late.status_code == 401 and code(late) == "invalid_credentials"
    assert sessions_of(engine, user) == 0


def test_registration_in_flight_never_overwrites_a_reset(client, engine, admin, in_flight):
    user, temporary = create_agency(client, admin)
    pending = session_for(client, AGENCY_EMAIL, temporary)
    body = {"password": OWN_PASSWORD, "profile": PROFILE}
    finish = in_flight("hash_password", lambda: client.post("/api/v1/auth/registration", json=body, headers=pending))
    reset = client.post(f"/api/v1/users/{user['id']}/temporary-password", headers=admin)
    assert reset.status_code == 200
    late = finish()  # the reset closed the session this registration came with
    assert late.status_code == 401 and code(late) == "not_authenticated"
    assert code(login(client, AGENCY_EMAIL, OWN_PASSWORD)) == "invalid_credentials"
    renewed = login(client, AGENCY_EMAIL, reset.json()["temporary_password"]).json()["user"]
    assert (renewed["status"], renewed["profile"], renewed["registered_at"]) == ("pendiente", None, None)


def test_reset_in_flight_is_applied_in_full_over_a_registration(client, engine, admin, in_flight):
    user, temporary = create_agency(client, admin)
    pending = session_for(client, AGENCY_EMAIL, temporary)
    path = f"/api/v1/users/{user['id']}/temporary-password"
    finish = in_flight("hash_password", lambda: client.post(path, headers=admin))
    body = {"password": OWN_PASSWORD, "profile": PROFILE}
    assert client.post("/api/v1/auth/registration", json=body, headers=pending).status_code == 204
    reset = finish()  # it read "pendiente" before hashing; the account became "activo" meanwhile
    assert reset.status_code == 200 and reset.json()["user"]["status"] == "pendiente"
    assert client.get(f"/api/v1/users/{user['id']}", headers=admin).json() == reset.json()["user"]
    assert reset.json()["user"]["profile"] == PROFILE and sessions_of(engine, user) == 0
    assert code(login(client, AGENCY_EMAIL, OWN_PASSWORD)) == "invalid_credentials"
    assert login(client, AGENCY_EMAIL, reset.json()["temporary_password"]).json()["user"]["status"] == "pendiente"


def test_one_pending_session_registers_only_once(client, engine, admin, in_flight):
    user, temporary = create_agency(client, admin)
    pending = session_for(client, AGENCY_EMAIL, temporary)
    first = {"password": "clave-primera-1", "profile": PROFILE}
    finish = in_flight("hash_password", lambda: client.post("/api/v1/auth/registration", json=first, headers=pending))
    second = {"password": "clave-segunda-2", "profile": {**PROFILE, "full_name": "Luis Soto"}}
    assert client.post("/api/v1/auth/registration", json=second, headers=pending).status_code == 204
    late = finish()
    assert late.status_code == 401 and code(late) == "not_authenticated"
    assert code(login(client, AGENCY_EMAIL, "clave-primera-1")) == "invalid_credentials"
    assert login(client, AGENCY_EMAIL, "clave-segunda-2").json()["user"]["profile"]["full_name"] == "Luis Soto"


def test_reset_in_flight_for_a_user_deleted_meanwhile(client, engine, admin, in_flight):
    user, _ = registered_agency(client, admin)
    path = f"/api/v1/users/{user['id']}"
    finish = in_flight("hash_password", lambda: client.post(f"{path}/temporary-password", headers=admin))
    assert client.delete(path, headers=admin).status_code == 204
    late = finish()
    assert late.status_code == 404 and code(late) == "user_not_found"
    assert [row["email"] for row in client.get("/api/v1/users", headers=admin).json()["users"]] == [ADMIN_EMAIL]


def test_duplicate_created_while_hashing_is_reported_as_existing(client, admin, in_flight):
    finish = in_flight("hash_password", lambda: client.post("/api/v1/users", json={"email": AGENCY_EMAIL}, headers=admin))
    assert client.post("/api/v1/users", json={"email": AGENCY_EMAIL.upper()}, headers=admin).status_code == 201
    late = finish()
    assert late.status_code == 409 and code(late) == "email_exists"
    assert len(client.get("/api/v1/users", headers=admin).json()["users"]) == 2


def test_session_closed_between_lookup_and_last_used_write(client, engine, admin, monkeypatch):
    with Session(engine) as db:
        db.execute(update(AuthSession).values(last_used_at=utcnow() - timedelta(minutes=5)))
        db.commit()
    real_now, pending = accounts.utcnow, [True]

    def now_after_logout():  # authenticate() asks for the time right after it found the session
        if pending and pending.pop():
            with Session(engine) as db:
                db.execute(delete(AuthSession))
                db.commit()
        return real_now()

    monkeypatch.setattr(accounts, "utcnow", now_after_logout)
    response = client.get("/api/v1/auth/me", headers=admin)
    assert response.status_code == 401 and code(response) == "not_authenticated"


def test_request_abandoned_by_the_timeout_writes_nothing(engine, monkeypatch):
    with TestClient(create_app(settings(request_timeout_seconds=0.3), engine=engine)) as client:
        admin = session_for(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        user, agency = registered_agency(client, admin)
        real, finished = accounts.hash_password, threading.Event()

        def slow_hash(password):
            time.sleep(0.9)  # the request is answered 503 long before the worker thread gets here
            try:
                return real(password)
            finally:
                finished.set()

        monkeypatch.setattr(accounts, "hash_password", slow_hash)
        for call in (lambda: client.post("/api/v1/users", json={"email": "otra@example.org"}, headers=admin),
                     lambda: client.post(f"/api/v1/users/{user['id']}/temporary-password", headers=admin)):
            finished.clear()
            response = call()
            assert response.status_code == 503 and code(response) == "request_timeout"
            assert finished.wait(10)
            time.sleep(0.3)  # let the abandoned thread reach, and be refused, its write
        monkeypatch.setattr(accounts, "hash_password", real)
        assert [row["email"] for row in client.get("/api/v1/users", headers=admin).json()["users"]] == [ADMIN_EMAIL, AGENCY_EMAIL]
        me = client.get("/api/v1/auth/me", headers=agency)  # neither half-reset nor logged out
        assert me.status_code == 200 and me.json()["status"] == "activo"
        assert login(client, AGENCY_EMAIL, OWN_PASSWORD).status_code == 200


@pytest.mark.parametrize("body", [b"\xff", b'{"email": "\xff"}', b'{"email": ' + b"1" * 5000 + b"}"])
def test_undecodable_body_is_a_malformed_request(client, admin, body):
    json_body = {"Content-Type": "application/json"}
    for method, path, headers in (("POST", "/api/v1/auth/login", json_body),
                                  ("POST", "/api/v1/auth/registration", {**admin, **json_body}),
                                  ("POST", "/api/v1/users", {**admin, **json_body}),
                                  ("PATCH", f"/api/v1/users/{UNKNOWN_ID}", {**admin, **json_body}),
                                  ("POST", f"/api/v1/users/{UNKNOWN_ID}/temporary-password", {**admin, **json_body})):
        response = client.request(method, path, content=body, headers=headers)
        assert response.status_code == 422 and code(response) == "invalid_request", (method, path)


def test_throttle_counts_per_client_and_stays_bounded():
    now = [0.0]
    throttle = security.LoginThrottle(attempts=2, window_seconds=10, max_keys=3, clock=lambda: now[0])
    key = ("a@example.org", "198.51.100.1")
    assert throttle.reserve(key) and throttle.reserve(key) and not throttle.reserve(key)
    assert throttle.reserve(("a@example.org", "198.51.100.2"))  # another client IP
    throttle.release(key)  # an attempt that ended without a verdict is not a failure
    assert throttle.reserve(key) and not throttle.reserve(key)
    throttle.clear(key)
    assert throttle.reserve(key)
    now[0] = 5.0
    assert throttle.reserve(key) and not throttle.reserve(key)
    now[0] = 10.5  # the first failure left the window, the second one did not
    assert throttle.reserve(key) and not throttle.reserve(key)
    for index in range(50):
        throttle.reserve((f"{index}@example.org", "198.51.100.3"))
    assert len(throttle._failures) == 3


def test_database_errors_do_not_count_as_failed_logins(client, engine):
    Base.metadata.drop_all(engine)  # the tables vanish after startup
    for _ in range(6):
        response = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert response.status_code == 503 and code(response) == "database_unavailable"
        assert "users" not in response.text and "sqlite" not in response.text.lower()
    assert client.post("/api/v1/analyze", json={}).status_code == 200


def test_responses_never_contain_hashes_or_stored_passwords(client, engine, admin):
    texts = []

    def keep(response):
        texts.append(response.text)
        return response

    created = keep(client.post("/api/v1/users", json={"email": AGENCY_EMAIL}, headers=admin)).json()
    user_path, temporary = f"/api/v1/users/{created['user']['id']}", created["temporary_password"]
    pending = bearer(keep(login(client, AGENCY_EMAIL, temporary)).json()["token"])
    keep(client.get("/api/v1/auth/me", headers=pending))
    keep(client.post("/api/v1/auth/registration", json={"password": temporary, "profile": PROFILE}, headers=pending))
    keep(client.post("/api/v1/auth/registration", json={"password": "corta-1", "profile": PROFILE}, headers=pending))
    keep(client.post("/api/v1/auth/registration", json={"password": OWN_PASSWORD, "profile": PROFILE}, headers=pending))
    active = bearer(keep(login(client, AGENCY_EMAIL, OWN_PASSWORD)).json()["token"])
    keep(login(client, AGENCY_EMAIL, "TEST-ONLY-incorrecta"))
    keep(client.get("/api/v1/auth/me", headers=active))
    keep(client.get("/api/v1/users", headers=admin))
    keep(client.get(user_path, headers=admin))
    keep(client.patch(user_path, json={"profile": PROFILE, "password": OWN_PASSWORD}, headers=admin))
    keep(client.patch(user_path, json={"email": "nueva@example.org"}, headers=admin))
    reset = keep(client.post(f"{user_path}/temporary-password", headers=admin)).json()["temporary_password"]
    keep(client.post(f"{user_path}/temporary-password", json={"temporary_password": "corta-1"}, headers=admin))
    keep(client.delete(user_path, headers=active))
    keep(client.post("/api/v1/auth/logout", headers=active))
    with Session(engine) as db:
        stored = db.scalars(select(User.password_hash)).all() + db.scalars(select(AuthSession.token_hash)).all()
    assert len(texts) == 17 and len(stored) >= 3
    joined = "\n".join(texts)
    assert "password_hash" not in joined and "token_hash" not in joined and "scrypt" not in joined
    assert not any(value in joined for value in stored)
    assert not any(value in joined for value in (ADMIN_PASSWORD, OWN_PASSWORD, "corta-1"))
    # Each temporary password appears once: in the response that issued it.
    assert [temporary in text for text in texts].count(True) == 1 and temporary in texts[0]
    assert [reset in text for text in texts].count(True) == 1


def test_secrets_are_never_cacheable(client, admin):
    user, _ = create_agency(client, admin)
    responses = [login(client, ADMIN_EMAIL, ADMIN_PASSWORD), client.get("/api/v1/auth/me", headers=admin),
                 client.get("/api/v1/users", headers=admin), client.get(f"/api/v1/users/{user['id']}", headers=admin),
                 client.post("/api/v1/users", json={"email": "otra@example.org"}, headers=admin),
                 client.patch(f"/api/v1/users/{user['id']}", json={"profile": PROFILE}, headers=admin),
                 client.post(f"/api/v1/users/{user['id']}/temporary-password", headers=admin)]
    assert [response.status_code for response in responses] == [200, 200, 200, 200, 201, 200, 200]
    assert all(response.headers["cache-control"] == "no-store" for response in responses)


def test_tokens_are_stored_only_as_digests(client, engine, admin):
    token = admin["Authorization"].removeprefix("Bearer ")
    assert len(token) == 43
    with Session(engine) as db:
        assert db.scalars(select(AuthSession.token_hash)).all() == [hashlib.sha256(token.encode()).hexdigest()]
    assert security.token_digest(token) == hashlib.sha256(token.encode()).hexdigest()
    assert security.new_token() != security.new_token()


def test_last_used_at_is_written_at_most_once_per_minute(client, engine, admin):
    def last_used():
        with Session(engine) as db:
            return db.scalar(select(AuthSession.last_used_at))

    created = last_used()
    assert created.utcoffset() == timedelta(0)  # SQLite returns naive values: they are read back as UTC
    assert client.get("/api/v1/auth/me", headers=admin).status_code == 200
    assert last_used() == created
    with Session(engine) as db:
        db.execute(update(AuthSession).values(last_used_at=created - timedelta(seconds=61)))
        db.commit()
    assert client.get("/api/v1/auth/me", headers=admin).status_code == 200
    refreshed = last_used()
    assert refreshed >= created
    assert client.get("/api/v1/auth/me", headers=admin).status_code == 200
    assert last_used() == refreshed


@pytest.mark.parametrize("method,path", [LOGIN] + SESSION_ROUTES + ADMIN_ROUTES)
def test_accounts_not_configured(method, path):
    with TestClient(create_app(Settings(environment="test"))) as client:
        for headers in ({}, bearer("desconocido")):
            response = call(client, method, path, headers)
            assert response.status_code == 503 and code(response) == "auth_not_configured"
        assert client.get("/health").json()["accounts_configured"] is False
        assert client.post("/api/v1/analyze", json={}).status_code == 200


def test_admin_seeding_is_idempotent_and_never_overwrites(engine):
    with TestClient(create_app(settings(), engine=engine)) as first:
        assert login(first, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200
    with Session(engine) as db:
        seeded = db.scalars(select(User)).all()
        assert [(row.email, row.role, row.status) for row in seeded] == [(ADMIN_EMAIL, "admin", "activo")]
        stored = seeded[0].password_hash
    # A restart with other credentials changes nothing while an admin exists.
    for changed in (settings(admin_password="TEST-ONLY-otra-clave"),
                    settings(admin_email="otro@example.org", admin_password="TEST-ONLY-otra-clave"), settings()):
        with TestClient(create_app(changed, engine=engine)) as restarted:
            assert login(restarted, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200
            assert code(login(restarted, changed.admin_email, "TEST-ONLY-otra-clave")) == "invalid_credentials"
    with Session(engine) as db:
        assert db.scalars(select(User.password_hash)).all() == [stored]


def test_no_admin_is_seeded_without_both_credentials(engine):
    for incomplete in (Settings(environment="test"), Settings(environment="test", admin_email=ADMIN_EMAIL),
                       Settings(environment="test", admin_password=ADMIN_PASSWORD)):
        with TestClient(create_app(incomplete, engine=engine)) as client:
            assert code(login(client, ADMIN_EMAIL, ADMIN_PASSWORD)) == "invalid_credentials"
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_seeding_never_promotes_an_existing_agency(client, engine, admin, caplog):
    create_agency(client, admin)
    with Session(engine) as db:
        db.execute(delete(User).where(User.role == "admin"))
        db.commit()
    with TestClient(create_app(settings(admin_email=AGENCY_EMAIL), engine=engine)) as restarted:
        assert code(login(restarted, AGENCY_EMAIL, ADMIN_PASSWORD)) == "invalid_credentials"
    with Session(engine) as db:
        assert db.scalars(select(User.role)).all() == ["agencia"]
    assert "ADMIN_EMAIL" in caplog.text and ADMIN_PASSWORD not in caplog.text


def test_unreachable_database_does_not_stop_the_api(tmp_path, monkeypatch):
    attempts = []
    real_init = accounts.init_schema
    monkeypatch.setattr(accounts, "init_schema", lambda engine: (attempts.append(1), real_init(engine)))
    engine = build_engine(f"sqlite:///{(tmp_path / 'missing' / 'accounts.db').as_posix()}")
    try:
        with TestClient(create_app(settings(), engine=engine)) as client:
            assert client.get("/health").json()["accounts_configured"] is True
            assert client.post("/api/v1/analyze", json={}).status_code == 200
            for _ in range(3):
                response = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
                assert response.status_code == 503 and code(response) == "database_unavailable"
                assert "missing" not in response.text and "sqlite" not in response.text.lower()
            assert len(attempts) == 1  # retries are spaced: an outage must not queue every request
            (tmp_path / "missing").mkdir()  # the database comes back
            client.app.state.accounts_retry_at = 0.0
            assert login(client, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200  # tables and admin created on demand
            assert len(attempts) == 2
    finally:
        engine.dispose()


def test_unreachable_postgres_answers_503_without_details(caplog):
    url = "postgresql://prometheus:TEST-ONLY-db-secret@127.0.0.1:1/accounts?connect_timeout=2"
    with TestClient(create_app(settings(database_url=url))) as client:
        assert client.app.state.db_engine.dialect.driver == "psycopg"
        response = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert response.status_code == 503 and code(response) == "database_unavailable"
        assert "TEST-ONLY-db-secret" not in response.text and "127.0.0.1" not in response.text
        assert client.get("/health").json()["accounts_configured"] is True
        assert client.post("/api/v1/analyze", json={}).status_code == 200
    assert "OperationalError" in caplog.text
    assert "TEST-ONLY-db-secret" not in caplog.text and "127.0.0.1" not in caplog.text


def test_postgres_engine_options(monkeypatch):
    seen = {}

    def connect(*args, **kwargs):
        seen.update(kwargs)
        raise psycopg.OperationalError("TEST ONLY")

    monkeypatch.setattr(psycopg, "connect", connect)
    engine = build_engine("postgres://prometheus:TEST-ONLY-db-secret@db.example.org/accounts?sslmode=require&channel_binding=require")
    assert engine.dialect.name == "postgresql" and engine.dialect.driver == "psycopg"
    assert (engine.pool.size(), engine.pool._max_overflow, engine.pool._recycle, engine.pool._pre_ping) == (3, 2, 300, True)
    assert "TEST-ONLY-db-secret" not in repr(engine) and "TEST-ONLY-db-secret" not in repr(engine.url)
    with pytest.raises(OperationalError):
        engine.connect()
    # PgBouncer in transaction mode: server-side prepared statements must stay disabled.
    assert "prepare_threshold" in seen and seen["prepare_threshold"] is None
    assert seen["connect_timeout"] == 10
    assert (seen["host"], seen["sslmode"], seen["channel_binding"]) == ("db.example.org", "require", "require")


@pytest.mark.parametrize("url,expected", [
    ("postgres://u:p@db.example.org/app", "postgresql+psycopg://u:p@db.example.org/app"),
    ("postgresql://u:p@db.example.org:5432/app?sslmode=require&channel_binding=require",
     "postgresql+psycopg://u:p@db.example.org:5432/app?sslmode=require&channel_binding=require"),
    ("  postgresql://u:p%40ss@db.example.org/app \n", "postgresql+psycopg://u:p%40ss@db.example.org/app"),
    ("postgresql+psycopg://u:p@db.example.org/app", "postgresql+psycopg://u:p@db.example.org/app"),
    ("sqlite://", "sqlite://"), ("sqlite:///accounts.db", "sqlite:///accounts.db"),
    ("sqlite+pysqlite:///:memory:", "sqlite+pysqlite:///:memory:"),
])
def test_normalize_database_url(url, expected):
    assert normalize_database_url(url) == expected


@pytest.mark.parametrize("url", ["", "TEST-ONLY-db-secret", "mysql://u:TEST-ONLY-db-secret@db.example.org/app",
                                 "postgresql+asyncpg://u:TEST-ONLY-db-secret@db.example.org/app",
                                 "postgresql://u:TEST-ONLY-db-secret@db.example.org:puerto/app"])
def test_unusable_database_urls_fail_without_echoing_the_value(url):
    with pytest.raises(ValueError) as error:
        build_engine(url)
    assert "TEST-ONLY-db-secret" not in str(error.value) and error.value.__cause__ is None
    assert error.value.__suppress_context__ or error.value.__context__ is None


def test_scrypt_hash_format_and_verification():
    stored = security.hash_password("contraseña-TEST")
    scheme, log2_n, r, p, salt, key = stored.split("$")
    assert (scheme, log2_n, r, p) == ("scrypt", "4", "8", "3")
    assert len(base64.b64decode(salt, validate=True)) == 16 and len(base64.b64decode(key, validate=True)) == 32
    assert security.verify_password("contraseña-TEST", stored) is True
    assert security.verify_password("contraseña-test", stored) is False
    assert security.verify_password("", stored) is False
    assert security.hash_password("contraseña-TEST") != stored  # random salt
    # A missing account burns the same verification and never matches, even with the dummy's own password.
    assert security.verify_password("TEST ONLY", security.DUMMY_HASH) is True
    assert security.verify_password("TEST ONLY", None) is False


def test_production_scrypt_cost(monkeypatch):
    assert PRODUCTION_COST == (15, 8, 3)
    assert (security.SALT_BYTES, security.KEY_BYTES) == (16, 32)
    scheme, log2_n, r, p, salt, key = PRODUCTION_DUMMY_HASH.split("$")
    assert (scheme, int(log2_n), int(r), int(p)) == ("scrypt", *PRODUCTION_COST)
    assert len(base64.b64decode(salt, validate=True)) == 16 and len(base64.b64decode(key, validate=True)) == 32
    monkeypatch.setattr(security, "SCRYPT_LOG2_N", PRODUCTION_COST[0])
    stored = security.hash_password("contraseña-TEST")  # fails if maxmem were too low for 32 MiB
    assert stored.startswith("scrypt$15$8$3$")
    monkeypatch.setattr(security, "SCRYPT_LOG2_N", 4)  # parameters come from the stored value, not the module
    assert security.verify_password("contraseña-TEST", stored) is True


@pytest.mark.parametrize("stored", [
    "", "sin-formato", "scrypt$4$8$3$c2FsdA==", "scrypt$4$8$3$c2FsdA==$a2V5$extra", "bcrypt$4$8$3$c2FsdA==$a2V5",
    "scrypt$x$8$3$c2FsdA==$a2V5", "scrypt$4$8$3$no es base64$a2V5", "scrypt$4$8$3$c2FsdA==$no es base64",
    "scrypt$4$8$3$c2FsdA==$", "scrypt$0$8$3$c2FsdA==$a2V5", "scrypt$-1$8$3$c2FsdA==$a2V5", "scrypt$99$8$3$c2FsdA==$a2V5",
    "scrypt$4$0$3$c2FsdA==$a2V5", "scrypt$4$8$0$c2FsdA==$a2V5", "scrypt$4$8$999$c2FsdA==$a2V5",
    "scrypt$20$32$1$c2FsdA==$a2V5", "scrypt$" + "9" * 5000 + "$8$3$c2FsdA==$a2V5", 12345, b"scrypt$4$8$3$c2FsdA==$a2V5",
])
def test_malformed_hashes_never_verify_or_raise(stored):
    assert security.verify_password("cualquiera", stored) is False


def test_settings_validate_and_never_expose_secrets():
    configured = Settings(database_url="postgresql://u:TEST-ONLY-db-secret@db.example.org/app",
                          admin_email=ADMIN_EMAIL, admin_password=ADMIN_PASSWORD)
    assert "TEST-ONLY-db-secret" not in repr(configured) and ADMIN_PASSWORD not in repr(configured)
    assert (configured.session_hours, configured.temporary_password_days, configured.min_password_length) == (12.0, 7.0, 8)
    for invalid in (dict(session_hours=0), dict(session_hours=-1), dict(session_hours=float("nan")),
                    dict(session_hours=float("inf")), dict(temporary_password_days=0), dict(min_password_length=0),
                    dict(min_password_length=129), dict(admin_email="TEST-ONLY-no-es-correo"),
                    dict(admin_password="corta-1"), dict(admin_password="x" * 129)):
        with pytest.raises(ValueError) as error:
            Settings(**invalid)
        assert "corta-1" not in str(error.value) and "TEST-ONLY" not in str(error.value)


def test_settings_from_env(monkeypatch):
    for name, value in {"DATABASE_URL": " postgresql://u:p@db.example.org/app ", "SESSION_HOURS": "2.5",
                        "TEMPORARY_PASSWORD_DAYS": "3", "ADMIN_EMAIL": " Admin@Example.org ",
                        "ADMIN_PASSWORD": ADMIN_PASSWORD}.items():
        monkeypatch.setenv(name, value)
    loaded = Settings.from_env()
    assert loaded.database_url == "postgresql://u:p@db.example.org/app"
    assert (loaded.session_hours, loaded.temporary_password_days) == (2.5, 3.0)
    assert (loaded.admin_email, loaded.admin_password) == ("Admin@Example.org", ADMIN_PASSWORD)
    monkeypatch.setenv("SESSION_HOURS", "doce")
    with pytest.raises(ValueError, match="SESSION_HOURS"):
        Settings.from_env()
    for name in ("DATABASE_URL", "SESSION_HOURS", "TEMPORARY_PASSWORD_DAYS", "ADMIN_EMAIL", "ADMIN_PASSWORD"):
        monkeypatch.setenv(name, "")
    empty = Settings.from_env()
    assert (empty.database_url, empty.admin_email, empty.admin_password, empty.session_hours) == (None, None, None, 12.0)


def test_env_file_parser(tmp_path, monkeypatch):
    text = ("# TEST ONLY\n\nAPP_ENV=test\n  DATABASE_URL = postgresql://u:p@db.example.org/app?sslmode=require&channel_binding=require  \n"
            "QUOTED=\"se conserva\"\nEMPTY=\n#COMMENTED=1\nHASH=valor # no es comentario\n")
    assert manage.parse_env_file(text) == {
        "APP_ENV": "test", "DATABASE_URL": "postgresql://u:p@db.example.org/app?sslmode=require&channel_binding=require",
        "QUOTED": '"se conserva"', "EMPTY": "", "HASH": "valor # no es comentario"}
    for line in ("TEST-ONLY-SECRET-SIN-IGUAL", "=TEST-ONLY-SECRET", "DOS PALABRAS=TEST-ONLY-SECRET"):
        with pytest.raises(ValueError, match="Línea 3") as error:
            manage.parse_env_file(f"# TEST ONLY\nOK=1\n{line}\n")
        assert "TEST-ONLY-SECRET" not in str(error.value)
    environ = {"APP_ENV": "production"}
    monkeypatch.setattr(os, "environ", environ)
    path = tmp_path / "test.env"
    path.write_bytes(b"\xef\xbb\xbfAPP_ENV=test\r\nSESSION_HOURS=3\r\n")  # BOM and CRLF, as Windows editors save
    manage.load_env_file(path)
    assert environ == {"APP_ENV": "production", "SESSION_HOURS": "3"}  # variables already set win


def test_manage_commands(tmp_path, monkeypatch, capsys):
    database = f"sqlite:///{(tmp_path / 'accounts.db').as_posix()}"
    path = tmp_path / "test.env"
    path.write_text(f"# TEST ONLY\nAPP_ENV=test\nDATABASE_URL={database}\nADMIN_EMAIL= Admin@Example.org \n"
                    f"ADMIN_PASSWORD={ADMIN_PASSWORD}\n", encoding="utf-8")

    def run(*arguments, environ=None):
        monkeypatch.setattr(os, "environ", {} if environ is None else environ)
        status = manage.main(["--env-file", str(path), *arguments])
        captured = capsys.readouterr()
        assert ADMIN_PASSWORD not in captured.out + captured.err and "accounts.db" not in captured.out + captured.err
        return status, captured.out + captured.err

    def stored():
        engine = build_engine(database)
        try:
            with Session(engine) as db:
                return db.execute(select(User.email, User.role, User.status, User.password_hash)).all()
        finally:
            engine.dispose()

    assert run("init-db") == (0, "Tablas listas: users, auth_sessions, reservations, place_overrides.\n")
    assert stored() == []
    assert run("admin") == (0, f"Administrador creado: {ADMIN_EMAIL}\n")
    (email, role, status, first_hash), = stored()
    assert (email, role, status) == (ADMIN_EMAIL, "admin", "activo")
    assert security.verify_password(ADMIN_PASSWORD, first_hash)
    assert run("admin") == (0, f"El administrador ya existe; no se modificó: {ADMIN_EMAIL}\n")
    assert run("init-db")[0] == 0 and stored()[0].password_hash == first_hash

    # --reset applies the configured password to the existing account and closes its sessions.
    engine = build_engine(database)
    try:
        with TestClient(create_app(settings(), engine=engine)) as client:
            old_session = session_for(client, ADMIN_EMAIL, ADMIN_PASSWORD)
            changed = {"ADMIN_PASSWORD": "TEST-ONLY-clave-rotada"}
            status, output = run("admin", "--reset", environ=changed)
            assert status == 0 and output == f"Contraseña del administrador restablecida y sesiones cerradas: {ADMIN_EMAIL}\n"
            assert "TEST-ONLY-clave-rotada" not in output
            assert code(client.get("/api/v1/auth/me", headers=old_session)) == "not_authenticated"
            assert code(login(client, ADMIN_EMAIL, ADMIN_PASSWORD)) == "invalid_credentials"
            create_agency(client, session_for(client, ADMIN_EMAIL, "TEST-ONLY-clave-rotada"))
    finally:
        engine.dispose()
    assert len(stored()) == 2 and stored()[0].password_hash != first_hash

    status, output = run("admin", "--reset", environ={"ADMIN_EMAIL": AGENCY_EMAIL})  # never promotes an agency
    assert status == 1 and AGENCY_EMAIL in output
    assert sorted(row.role for row in stored()) == ["admin", "agencia"]
    assert run("admin", environ={"ADMIN_PASSWORD": "corta-1"})[0] == 2
    assert run("admin", environ={"DATABASE_URL": "mysql://u:TEST-ONLY-db-secret@db.example.org/app"}) == (
        2, "DATABASE_URL debe ser una URL postgresql://.\n")
    path.write_text("APP_ENV=test\n", encoding="utf-8")
    assert run("init-db") == (2, "DATABASE_URL no está configurada.\n")
    assert run("admin", environ={"DATABASE_URL": database}) == (2, "Este comando requiere ADMIN_EMAIL y ADMIN_PASSWORD.\n")
    path.unlink()
    assert run("init-db") == (2, "No se pudo leer el archivo de entorno.\n")


def test_manage_reports_an_unreachable_database_by_class_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", {"DATABASE_URL": f"sqlite:///{(tmp_path / 'missing' / 'accounts.db').as_posix()}"})
    assert manage.main(["init-db"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "Base de datos no disponible (OperationalError).\n"


def test_cors_allows_account_methods_and_authorization_header(engine):
    origin = "https://prometheus-example.vercel.app"
    with TestClient(create_app(settings(environment="production", cors_origins=(origin,)), engine=engine)) as client:
        preflight = {"Origin": origin, "Access-Control-Request-Headers": "authorization,content-type"}
        for method, path in [LOGIN] + SESSION_ROUTES + ADMIN_ROUTES:
            response = client.options(path, headers={**preflight, "Access-Control-Request-Method": method})
            assert response.status_code == 200, (method, path)
            assert response.headers["access-control-allow-origin"] == origin
            assert "access-control-allow-credentials" not in response.headers
        assert client.options(ADMIN_ROUTES[0][1], headers={**preflight, "Access-Control-Request-Method": "PUT"}).status_code == 400
        assert client.options(ADMIN_ROUTES[0][1], headers={**preflight, "Access-Control-Request-Method": "GET",
                                                            "Access-Control-Request-Headers": "x-api-key"}).status_code == 400
        error = client.get("/api/v1/auth/me", headers={"Origin": origin})
        assert error.status_code == 401 and error.headers["access-control-allow-origin"] == origin
        assert "access-control-allow-origin" not in client.get("/api/v1/auth/me", headers={"Origin": "https://other.vercel.app"}).headers


def test_openapi_documents_accounts(client):
    schema = client.get("/openapi.json").json()
    for method, path in [LOGIN] + SESSION_ROUTES + ADMIN_ROUTES:
        assert method.lower() in schema["paths"][path.replace(UNKNOWN_ID, "{id}")]
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert "password_hash" not in str(schema)
