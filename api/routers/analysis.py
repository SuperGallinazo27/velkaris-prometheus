import asyncio

from fastapi import APIRouter, Request

from api.connectors.registry import CONNECTORS
from api.schemas import AnalyzeRequest, AnalyzeResponse, ErrorResponse
from api.schemas import EarthEngineQuery
from api.gee_schemas import LocationsResponse
from api.services.live_analysis import analyze_earth_engine
from api.services.analyze import analyze_descriptors
from api.services.datasets import load_descriptors

router = APIRouter(prefix="/api/v1", tags=["analysis"])


@router.post("/analyze", response_model=AnalyzeResponse, responses={
    422: {"model": ErrorResponse, "description": "Solicitud inválida o descriptores requeridos ausentes."},
    503: {"model": ErrorResponse, "description": "Fuente ausente, inválida, no implementada o tiempo agotado."},
    413: {"model": ErrorResponse, "description": "Cuerpo demasiado grande."},
    429: {"model": ErrorResponse, "description": "Límite de consultas Earth Engine."},
})
async def analyze(payload: AnalyzeRequest, request: Request):
    async with request.app.state.analysis_slots:
        if payload.source == "google_earth_engine":
            return await analyze_earth_engine(payload, request.app.state.settings, request.app.state.earth_engine)
        if payload.data_mode == "live":
            dataset = await CONNECTORS[payload.source].descriptors(payload)
        else:
            dataset = await asyncio.to_thread(load_descriptors, payload.data_mode, request.app.state.settings)
        return analyze_descriptors(payload, dataset)


@router.get("/sources")
async def sources(request: Request):
    return {"sources": [connector.status() for connector in CONNECTORS.values()] + [request.app.state.earth_engine.status()]}


@router.post("/earth-engine/locations", response_model=LocationsResponse,
             responses={503: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 422: {"model": ErrorResponse}})
async def earth_locations(payload: EarthEngineQuery, request: Request):
    return await request.app.state.earth_engine.sample(payload)
