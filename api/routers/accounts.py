"""Authentication and user administration. Handlers are synchronous on purpose: SQLAlchemy and
scrypt block, so FastAPI runs them in its thread pool instead of on the event loop."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from api.account_schemas import (
    CreateUserRequest, LoginRequest, LoginResponse, RegistrationRequest, TemporaryPasswordRequest,
    TemporaryPasswordResponse, UpdateUserRequest, UserList, UserResponse,
)
from api.errors import AnalysisError
from api.models import ACTIVE, ADMIN, User
from api.schemas import ErrorResponse
from api.services import accounts

bearer = HTTPBearer(auto_error=False, description="Token devuelto por POST /api/v1/auth/login.")


def no_store(response: Response) -> None:
    # Tokens, temporary passwords and personal data must not be written to any HTTP cache.
    response.headers["Cache-Control"] = "no-store"


class AccountRoute(APIRoute):
    """A body that cannot even be decoded (invalid UTF-8, absurd number literals) makes FastAPI
    answer 400 in its own shape; here it is reported like any other malformed body."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except HTTPException as exc:
                if exc.status_code != 400:
                    raise
                raise RequestValidationError([{"loc": ("body",), "type": "value_error",
                                               "msg": "Cuerpo ilegible; se esperaba JSON en UTF-8."}]) from None

        return handle


router = APIRouter(prefix="/api/v1", route_class=AccountRoute, dependencies=[Depends(no_store)])


def errors(*statuses: int) -> dict:
    return {status: {"model": ErrorResponse} for status in statuses}


def database(request: Request) -> Iterator[Session]:
    state = request.app.state
    if state.db_sessions is None:
        raise AnalysisError(503, "auth_not_configured",
                            "Las cuentas no están configuradas en este servidor: falta DATABASE_URL.")
    if not accounts.prepare(state):
        raise AnalysisError(503, "database_unavailable", accounts.DATABASE_UNAVAILABLE)
    with state.db_sessions() as db:
        yield db


Database = Annotated[Session, Depends(database)]


def identity(db: Database,
             credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> accounts.Identity:
    return accounts.authenticate(db, credentials.credentials.strip() if credentials else None)


Current = Annotated[accounts.Identity, Depends(identity)]


def admin(current: Current) -> User:
    # A pending account may only read itself, log out and complete its registration.
    if current.user.status != ACTIVE or current.user.role != ADMIN:
        raise AnalysisError(403, "forbidden", "No tiene permiso para realizar esta operación.")
    return current.user


Admin = Annotated[User, Depends(admin)]
UserId = Annotated[str, Path(alias="id")]


@router.post("/auth/login", response_model=LoginResponse, tags=["auth"], responses=errors(401, 422, 429, 503))
def login(payload: LoginRequest, request: Request, db: Database):
    state = request.app.state
    token, session, user = accounts.login(db, state.settings, state.login_throttle, payload.email, payload.password,
                                          request.client.host if request.client else "unknown")
    return LoginResponse(token=token, expires_at=accounts.iso(session.expires_at), user=accounts.user_response(user))


@router.post("/auth/logout", status_code=204, tags=["auth"], responses=errors(401, 503))
def logout(db: Database, current: Current):
    accounts.logout(db, current.session)
    return Response(status_code=204)


@router.get("/auth/me", response_model=UserResponse, tags=["auth"], responses=errors(401, 503))
def me(current: Current):
    return accounts.user_response(current.user)


@router.post("/auth/registration", status_code=204, tags=["auth"], responses=errors(401, 409, 422, 503))
def registration(payload: RegistrationRequest, request: Request, db: Database, current: Current):
    accounts.complete_registration(db, request.app.state.settings, current.user, payload.password, payload.profile)
    return Response(status_code=204)


@router.get("/users", response_model=UserList, tags=["users"], responses=errors(401, 403, 503))
def list_users(db: Database, _: Admin):
    return UserList(users=[accounts.user_response(user) for user in accounts.list_users(db)])


@router.post("/users", response_model=TemporaryPasswordResponse, status_code=201, tags=["users"],
             responses=errors(401, 403, 409, 422, 503))
def create_user(payload: CreateUserRequest, request: Request, db: Database, _: Admin):
    user, password = accounts.create_user(db, request.app.state.settings, payload.email, payload.temporary_password)
    return TemporaryPasswordResponse(user=accounts.user_response(user), temporary_password=password)


@router.get("/users/{id}", response_model=UserResponse, tags=["users"], responses=errors(401, 403, 404, 503))
def get_user(user_id: UserId, db: Database, _: Admin):
    return accounts.user_response(accounts.find_user(db, user_id))


@router.patch("/users/{id}", response_model=UserResponse, tags=["users"],
              responses=errors(401, 403, 404, 409, 422, 503))
def update_user(user_id: UserId, payload: UpdateUserRequest, db: Database, _: Admin):
    return accounts.user_response(accounts.update_user(db, user_id, payload.email, payload.profile))


@router.delete("/users/{id}", status_code=204, tags=["users"], responses=errors(401, 403, 404, 409, 503))
def delete_user(user_id: UserId, db: Database, actor: Admin):
    accounts.delete_user(db, actor, user_id)
    return Response(status_code=204)


@router.post("/users/{id}/temporary-password", response_model=TemporaryPasswordResponse, tags=["users"],
             responses=errors(401, 403, 404, 409, 422, 503))
def reset_temporary_password(user_id: UserId, request: Request, db: Database, _: Admin,
                             payload: TemporaryPasswordRequest | None = None):
    user, password = accounts.reset_temporary_password(db, request.app.state.settings, user_id,
                                                       payload.temporary_password if payload else None)
    return TemporaryPasswordResponse(user=accounts.user_response(user), temporary_password=password)
