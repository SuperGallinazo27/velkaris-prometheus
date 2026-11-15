import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from api.config import Settings
from api.db import build_engine, error_summary
from api.errors import AnalysisError
from api.middleware import ResourceLimitsMiddleware
from api.routers.accounts import router as accounts_router
from api.routers.analysis import router
from api.routers.assistant import router as assistant_router
from api.routers.climate import router as climate_router
from api.routers.reservations import router as reservations_router
from api.security import LoginThrottle
from api.services import accounts
from api.services.assistant import AssistantService
from api.services.earth_engine import EarthEngineService
from api.services.enso import EnsoService

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    """`engine` injects a ready database (tests use in-memory SQLite) instead of DATABASE_URL."""
    settings = settings or Settings.from_env()
    owns_engine = engine is None and settings.database_url is not None
    if owns_engine:
        engine = build_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if engine is not None:
            # Tables and first admin. If the database is unreachable the accounts answer 503 and
            # retry on later requests; the public analysis API must start regardless.
            await asyncio.to_thread(accounts.prepare, app.state)
        yield
        if owns_engine:
            engine.dispose()

    app = FastAPI(title="PROMETHEUS API", version="1.0.0", lifespan=lifespan,
                  description="Análogos exploratorios con demo sintética explícita; sin porcentajes calibrados.")
    app.state.settings = settings
    app.state.earth_engine = EarthEngineService(settings)
    app.state.analysis_slots = asyncio.Semaphore(4)
    app.state.db_engine = engine
    # close_resets_only=False: a handler thread that outlives its cancelled request (timeout)
    # finds its session permanently closed and cannot commit partial work.
    app.state.db_sessions = (sessionmaker(engine, expire_on_commit=False, close_resets_only=False)
                             if engine is not None else None)
    app.state.accounts_ready, app.state.accounts_retry_at = False, 0.0
    # In memory and per app instance: correct for the single worker this service runs with.
    app.state.login_throttle = LoginThrottle()
    # Copilot (Gemini) and El Niño index (NOAA): both talk to an outside service and keep their own limits.
    app.state.assistant = AssistantService(settings)
    app.state.enso = EnsoService()
    app.add_middleware(ResourceLimitsMiddleware, max_body_bytes=settings.max_body_bytes,
                       timeout_seconds=settings.request_timeout_seconds)
    # Outermost user middleware so controlled errors also carry CORS headers.
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_credentials=False, allow_methods=["GET", "POST", "PATCH", "DELETE"],
                       allow_headers=["Content-Type", "Authorization"], max_age=600)

    @app.exception_handler(AnalysisError)
    async def analysis_error(request: Request, exc: AnalysisError):
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": {"code": exc.code, "message": exc.message, **exc.extra}},
                            headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Do not echo arbitrary input or non-finite floats back into JSON.
        issues = "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors())
        return JSONResponse(status_code=422, content={"detail": {
            "code": "invalid_request", "message": issues,
        }})

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        # Driver messages can carry hosts and statements: log a summary, answer without details.
        logger.error("Error de base de datos en cuentas (%s).", error_summary(exc))
        return JSONResponse(status_code=503, content={"detail": {
            "code": "database_unavailable", "message": accounts.DATABASE_UNAVAILABLE,
        }})

    @app.get("/health", tags=["operations"])
    async def health():
        return {"status": "ok", "default_data_mode": "demo", "live_sources_ready": False,
                "earth_engine_configured": app.state.earth_engine.status()["status"] == "configured_unverified",
                "accounts_configured": app.state.db_sessions is not None,
                "assistant_configured": app.state.assistant.configured}

    app.include_router(router)
    app.include_router(climate_router)
    app.include_router(assistant_router)
    app.include_router(accounts_router)
    app.include_router(reservations_router)
    return app


app = create_app()
