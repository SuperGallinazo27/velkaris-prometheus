"""Climate context that the browser cannot fetch by itself. Public and read-only, like the analysis API."""

from fastapi import APIRouter, Request, Response

from api.schemas import ErrorResponse

router = APIRouter(prefix="/api/v1/climate", tags=["climate"])


@router.get("/enso", responses={503: {"model": ErrorResponse, "description": "NOAA no respondió y no hay lectura previa."}})
async def enso(request: Request, response: Response):
    """Últimos cinco trimestres del índice ONI y la temperatura semanal del mar en las regiones Niño (NOAA CPC)."""
    reading = await request.app.state.enso.reading()
    response.headers["Cache-Control"] = "public, max-age=3600"
    return reading
