"""Copilot endpoint. Only active accounts may use it: every call spends the server's Gemini quota."""

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from api.errors import AnalysisError
from api.models import ACTIVE
from api.schemas import ErrorResponse, StrictModel
from api.services import accounts
from api.services.assistant import MAX_QUESTION_CHARS

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])
bearer = HTTPBearer(auto_error=False, description="Token devuelto por POST /api/v1/auth/login.")


class AssistantRequest(StrictModel):
    kind: Literal["explicar", "pregunta", "plan"]
    question: Annotated[str, Field(max_length=MAX_QUESTION_CHARS)] | None = None
    # What the screen already shows about the place or the plan. Bounded by the body limit of the API.
    context: dict[str, Any]


class AssistantResponse(StrictModel):
    answer: str
    model: str


def active_user_id(state, token: str | None) -> str:
    """Opens and closes its own database session, so no connection is held while the model answers."""
    if state.db_sessions is None:
        raise AnalysisError(503, "auth_not_configured",
                            "Las cuentas no están configuradas en este servidor: falta DATABASE_URL.")
    if not accounts.prepare(state):
        raise AnalysisError(503, "database_unavailable", accounts.DATABASE_UNAVAILABLE)
    with state.db_sessions() as db:
        user = accounts.authenticate(db, token).user
        if user.status != ACTIVE:
            raise AnalysisError(403, "forbidden", "No tiene permiso para realizar esta operación.")
        return user.id


@router.post("/ask", response_model=AssistantResponse,
             responses={status: {"model": ErrorResponse} for status in (401, 403, 422, 429, 503)})
async def ask(payload: AssistantRequest, request: Request, response: Response,
              credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    """Explica un lugar, responde una pregunta o redacta el resumen de un plan, solo con los datos enviados."""
    state = request.app.state
    user_id = await asyncio.to_thread(active_user_id, state, credentials.credentials.strip() if credentials else None)
    response.headers["Cache-Control"] = "no-store"
    return await state.assistant.ask(user_id, payload.kind, payload.question, payload.context)
