import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from api import security
from api.config import Settings
from api.db import build_engine
from api.errors import AnalysisError
from api.main import create_app
from api.services import enso as enso_service
from api.services.assistant import (
    MAX_CONTEXT_CHARS, SYSTEM, AssistantService, HourlyLimit, build_prompt, clean,
)
from api.services.enso import EnsoService, parse_oni, parse_weekly

# Every credential below is a TEST ONLY fixture.
ADMIN_EMAIL = "admin@example.org"
ADMIN_PASSWORD = "TEST-ONLY-admin-password"
TEST_KEY = "TEST-ONLY-gemini-key"
ASK = "/api/v1/assistant/ask"
CONTEXT = {"lugar": "Sierra Negra", "coincidencia": 91, "lluvia_30_dias_mm": 426}

ONI_TEXT = """SEAS  YR   TOTAL   ANOM
 DJF 2026  26.40  -0.35
 JFM 2026  26.57  -0.21
 FMA 2026  27.34   0.11
 MAM 2026  28.09   0.46
 AMJ 2026  28.74   0.95
 MJJ 2026  29.02   1.39
"""
WEEKLY_TEXT = """ Weekly SST data starts week centered on 3Jan1990

                Nino1+2      Nino3        Nino34        Nino4
 Week          SST SSTA     SST SSTA     SST SSTA     SST SSTA
 03JAN1990     23.4-0.4     25.1-0.3     26.6 0.0     28.6 0.3
 04NOV2026     27.0 5.6     29.5 4.4     30.1 3.3     30.1 1.5
"""


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    monkeypatch.setattr(security, "SCRYPT_LOG2_N", 4)
    monkeypatch.setattr(security, "DUMMY_HASH", security.hash_password("TEST ONLY"))


@pytest.fixture
def engine():
    instance = build_engine("sqlite://")
    yield instance
    instance.dispose()


def settings(**overrides):
    return Settings(**{"environment": "test", "admin_email": ADMIN_EMAIL, "admin_password": ADMIN_PASSWORD,
                       "gemini_api_key": TEST_KEY, **overrides})


def gemini(text="Respuesta de prueba.", status=200, finish="STOP"):
    """A stand-in for Google that records what it was sent."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "upstream detail that must not leak"}})
        parts = [{"text": text}] if text else []
        return httpx.Response(200, json={"candidates": [{"content": {"parts": parts}, "finishReason": finish}]})

    return httpx.MockTransport(handler), calls


def client_with(engine, transport, **overrides):
    config = settings(**overrides)
    app = create_app(config, engine=engine)
    app.state.assistant = AssistantService(config, transport=transport)
    return TestClient(app)


def login(client) -> dict:
    token = client.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_ask_requires_a_session(engine):
    transport, calls = gemini()
    with client_with(engine, transport) as client:
        response = client.post(ASK, json={"kind": "explicar", "context": CONTEXT})
    assert response.status_code == 401 and response.json()["detail"]["code"] == "not_authenticated"
    assert not calls


def test_ask_answers_from_the_context_and_keeps_the_key_in_a_header(engine):
    transport, calls = gemini("  **Sí**, pero ahora llueve mucho.\n# Título  ")
    with client_with(engine, transport) as client:
        response = client.post(ASK, headers=login(client),
                               json={"kind": "pregunta", "question": " ¿Conviene ir ahora? ", "context": CONTEXT})
    assert response.status_code == 200, response.text
    assert response.json() == {"answer": "Sí, pero ahora llueve mucho.\nTítulo", "model": "gemini-3.5-flash"}
    assert response.headers["cache-control"] == "no-store"
    sent = calls[0]
    assert sent.headers["x-goog-api-key"] == TEST_KEY
    assert TEST_KEY not in str(sent.url) and sent.url.path.endswith("/models/gemini-3.5-flash:generateContent")
    body = json.loads(sent.content)
    assert body["systemInstruction"]["parts"][0]["text"] == SYSTEM
    prompt = body["contents"][0]["parts"][0]["text"]
    assert '"lugar":"Sierra Negra"' in prompt and prompt.endswith("PREGUNTA: ¿Conviene ir ahora?")
    # Nothing about the person asking travels to the model.
    assert ADMIN_EMAIL not in sent.content.decode()


def test_question_is_required_only_for_questions(engine):
    transport, calls = gemini()
    with client_with(engine, transport) as client:
        headers = login(client)
        missing = client.post(ASK, headers=headers, json={"kind": "pregunta", "question": "  ", "context": CONTEXT})
        explained = client.post(ASK, headers=headers, json={"kind": "explicar", "context": CONTEXT})
        unknown = client.post(ASK, headers=headers, json={"kind": "otro", "context": CONTEXT})
        long = client.post(ASK, headers=headers, json={"kind": "pregunta", "question": "x" * 601, "context": CONTEXT})
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "question_required"
    assert explained.status_code == 200
    assert unknown.status_code == 422 and long.status_code == 422
    assert len(calls) == 1


def test_not_configured_without_a_key(engine):
    transport, calls = gemini()
    with client_with(engine, transport, gemini_api_key=None) as client:
        response = client.post(ASK, headers=login(client), json={"kind": "explicar", "context": CONTEXT})
        health = client.get("/health").json()
    assert response.status_code == 503 and response.json()["detail"]["code"] == "assistant_not_configured"
    assert health["assistant_configured"] is False and not calls


def test_falls_back_to_the_lighter_model_and_never_leaks_upstream_text(engine):
    answers = iter([httpx.Response(503, json={"error": {"message": "secret upstream detail"}}),
                    httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Hola."}]}}]})])
    models = []

    def handler(request):
        models.append(request.url.path.split("/models/")[1].split(":")[0])
        return next(answers)

    with client_with(engine, httpx.MockTransport(handler)) as client:
        response = client.post(ASK, headers=login(client), json={"kind": "explicar", "context": CONTEXT})
    assert response.json() == {"answer": "Hola.", "model": "gemini-flash-lite-latest"}
    assert models == ["gemini-3.5-flash", "gemini-flash-lite-latest"]


@pytest.mark.parametrize("status, code, http", [(429, "assistant_quota", 429), (403, "assistant_unavailable", 503),
                                                (500, "assistant_unavailable", 503)])
def test_upstream_errors_become_controlled_errors(engine, status, code, http):
    transport, _ = gemini(status=status)
    with client_with(engine, transport) as client:
        response = client.post(ASK, headers=login(client), json={"kind": "explicar", "context": CONTEXT})
    assert response.status_code == http and response.json()["detail"]["code"] == code
    assert "upstream detail" not in response.text and TEST_KEY not in response.text


def test_spent_quota_on_the_main_model_falls_back_to_the_lighter_one(engine):
    # The free tier gives the main model very few requests per day; each model has its own quota.
    models = []

    def handler(request):
        model = request.url.path.split("/models/")[1].split(":")[0]
        models.append(model)
        if model == "gemini-3.5-flash":
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota detail"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Hola."}]}}]})

    with client_with(engine, httpx.MockTransport(handler)) as client:
        response = client.post(ASK, headers=login(client), json={"kind": "explicar", "context": CONTEXT})
    assert response.status_code == 200
    assert response.json() == {"answer": "Hola.", "model": "gemini-flash-lite-latest"}
    assert models == ["gemini-3.5-flash", "gemini-flash-lite-latest"]


def test_retries_without_the_reasoning_setting_when_a_model_rejects_it(engine):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body["generationConfig"])
        if "thinkingConfig" in body["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "Request contains an invalid argument."}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Hola."}]}}]})

    with client_with(engine, httpx.MockTransport(handler)) as client:
        response = client.post(ASK, headers=login(client), json={"kind": "explicar", "context": CONTEXT})
    assert response.json() == {"answer": "Hola.", "model": "gemini-3.5-flash"}
    assert ["thinkingConfig" in config for config in bodies] == [True, False]


def test_blocked_answer_and_oversized_context(engine):
    transport, calls = gemini(text="", finish="SAFETY")
    with client_with(engine, transport, gemini_fallback_model=None) as client:
        headers = login(client)
        blocked = client.post(ASK, headers=headers, json={"kind": "explicar", "context": CONTEXT})
    assert blocked.status_code == 422 and blocked.json()["detail"]["code"] == "assistant_blocked"
    with pytest.raises(AnalysisError) as error:
        build_prompt("explicar", None, {"texto": "x" * MAX_CONTEXT_CHARS})
    assert error.value.code == "context_too_large"


def test_hourly_limit_counts_only_answered_requests(engine):
    transport, _ = gemini()
    with client_with(engine, transport, assistant_hourly_limit=2) as client:
        headers = login(client)
        statuses = [client.post(ASK, headers=headers, json={"kind": "explicar", "context": CONTEXT}).status_code
                    for _ in range(3)]
    assert statuses == [200, 200, 429]

    failing, _ = gemini(status=500)
    with client_with(engine, failing, assistant_hourly_limit=1) as client:
        headers = login(client)
        codes = [client.post(ASK, headers=headers, json={"kind": "explicar", "context": CONTEXT}).json()["detail"]["code"]
                 for _ in range(2)]
    # A failure of the model does not use up the account's allowance.
    assert codes == ["assistant_unavailable", "assistant_unavailable"]


def test_hourly_limit_window():
    now = [0.0]
    limit = HourlyLimit(2, window_seconds=10, clock=lambda: now[0])
    assert limit.allow("a") and limit.allow("a") and not limit.allow("a") and limit.allow("b")
    now[0] = 11
    assert limit.allow("a")


def test_clean_removes_markdown():
    assert clean("## Resumen\n**Uno** y __dos__\n\n  final  ") == "Resumen\nUno y dos\n\nfinal"


def test_settings_reject_bad_models_and_hide_the_key():
    with pytest.raises(ValueError):
        Settings(gemini_model="../otra/ruta")
    assert TEST_KEY not in repr(settings())


# ---------- El Niño (NOAA) ----------

def noaa(oni=ONI_TEXT, weekly=WEEKLY_TEXT, status=200):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, text=oni if request.url.path.endswith("oni.ascii.txt") else weekly)

    return httpx.MockTransport(handler), calls


def test_parse_noaa_tables():
    recent = parse_oni(ONI_TEXT)
    assert len(recent) == 5 and recent[-1] == {"season": "MJJ", "year": 2026, "oni": 1.39}
    assert recent[0] == {"season": "JFM", "year": 2026, "oni": -0.21}
    # Negative anomalies touch the previous column in the weekly table.
    assert parse_weekly(WEEKLY_TEXT.replace(" 04NOV2026     27.0 5.6", " 04NOV2026     27.0-5.6"))["nino12"]["anomaly"] == -5.6
    assert parse_weekly(WEEKLY_TEXT) == {"week": "2026-11-04", "nino12": {"sst": 27.0, "anomaly": 5.6},
                                         "nino34": {"sst": 30.1, "anomaly": 3.3}}
    with pytest.raises(ValueError):
        parse_oni("sin filas")


def test_enso_endpoint_is_public_and_cached(engine):
    transport, calls = noaa()
    app = create_app(settings(), engine=engine)
    app.state.enso = EnsoService(transport=transport)
    with TestClient(app) as client:
        first = client.get("/api/v1/climate/enso")
        second = client.get("/api/v1/climate/enso")
    assert first.status_code == 200 and first.json() == second.json()
    body = first.json()
    assert body["recent"][-1]["oni"] == 1.39 and body["weekly"]["nino12"]["anomaly"] == 5.6
    assert body["source"]["oni"] == enso_service.ONI_URL and len(body["fetched_at"]) == 10
    assert first.headers["cache-control"] == "public, max-age=3600"
    assert len(calls) == 2  # one read of each table, reused by the second request


def test_enso_survives_a_missing_weekly_table_and_reports_an_outage():
    transport, _ = noaa(weekly="tabla ilegible")
    assert asyncio.run(EnsoService(transport=transport).reading())["weekly"] is None

    down, calls = noaa(status=500)
    service = EnsoService(transport=down)
    for _ in range(2):
        with pytest.raises(AnalysisError) as error:
            asyncio.run(service.reading())
        assert error.value.status_code == 503 and error.value.code == "enso_unavailable"
    assert len(calls) == 2  # the failed read is not repeated at once


def test_enso_serves_the_last_reading_when_noaa_goes_down():
    now = [0.0]
    state = {"status": 200}

    def handler(request):
        if state["status"] != 200:
            return httpx.Response(500)
        return httpx.Response(200, text=ONI_TEXT if request.url.path.endswith("oni.ascii.txt") else WEEKLY_TEXT)

    async def scenario():
        service = EnsoService(transport=httpx.MockTransport(handler), clock=lambda: now[0])
        first = await service.reading()
        state["status"], now[0] = 500, enso_service.CACHE_SECONDS + 1
        return first, await service.reading()

    first, later = asyncio.run(scenario())
    assert later == first
