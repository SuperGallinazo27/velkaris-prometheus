import asyncio

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.config import Settings
from api.errors import AnalysisError
from api.middleware import ResourceLimitsMiddleware
from api.routers.analysis import router
from api.services.earth_engine import EarthEngineService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="PROMETHEUS API", version="1.0.0",
                  description="Análogos exploratorios con demo sintética explícita; sin porcentajes calibrados.")
    app.state.settings = settings
    app.state.earth_engine = EarthEngineService(settings)
    app.state.analysis_slots = asyncio.Semaphore(4)
    app.add_middleware(ResourceLimitsMiddleware, max_body_bytes=settings.max_body_bytes,
                       timeout_seconds=settings.request_timeout_seconds)
    # Outermost user middleware so controlled errors also carry CORS headers.
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_credentials=False, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"], max_age=600)

    @app.exception_handler(AnalysisError)
    async def analysis_error(request: Request, exc: AnalysisError):
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Do not echo arbitrary input or non-finite floats back into JSON.
        issues = "; ".join(f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors())
        return JSONResponse(status_code=422, content={"detail": {
            "code": "invalid_request", "message": issues,
        }})

    @app.get("/health", tags=["operations"])
    async def health():
        return {"status": "ok", "default_data_mode": "demo", "live_sources_ready": False,
                "earth_engine_configured": app.state.earth_engine.status()["status"] == "configured_unverified"}

    app.include_router(router)
    return app


app = create_app()
