import asyncio

from fastapi import APIRouter, Request

from api.connectors.registry import CONNECTORS
from api.schemas import AnalyzeRequest, AnalyzeResponse, ErrorResponse
from api.services.analyze import analyze_descriptors
from api.services.datasets import load_descriptors

router = APIRouter(prefix="/api/v1", tags=["analysis"])


@router.post("/analyze", response_model=AnalyzeResponse, responses={
    422: {"model": ErrorResponse, "description": "Solicitud inválida o descriptores requeridos ausentes."},
    503: {"model": ErrorResponse, "description": "Fuente ausente, inválida, no implementada o tiempo agotado."},
    413: {"model": ErrorResponse, "description": "Cuerpo demasiado grande."},
})
async def analyze(payload: AnalyzeRequest, request: Request):
    async with request.app.state.analysis_slots:
        if payload.data_mode == "live":
            dataset = await CONNECTORS[payload.source].descriptors(payload)
        else:
            dataset = await asyncio.to_thread(load_descriptors, payload.data_mode, request.app.state.settings)
        return analyze_descriptors(payload, dataset)


@router.get("/sources")
async def sources():
    return {"sources": [connector.status() for connector in CONNECTORS.values()]}
