import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from api.config import ROOT, Settings
from api.connectors.registry import CONNECTORS
from api.main import create_app
from api.schemas import AnalyzeRequest, DescriptorDataset
from api.services.analyze import analyze_descriptors
from api.services.datasets import load_descriptors
from prometheus.data import load_dataset
from prometheus.ranking import rank_candidates


@pytest.fixture
def client():
    with TestClient(create_app(Settings(environment="test"))) as instance:
        yield instance


@pytest.fixture
def external_data():
    # Synthetic TEST fixture only. Not shipped as purported scientific measurements.
    data = load_descriptors("demo", Settings()).model_dump()
    data["mode"] = "external"
    for row in data["targets"] + data["candidates"]:
        row["provenance"] = dict(source="TEST ONLY", product_id="TEST", method="TEST",
                                 resolution_m=30, window_m=1000, reference_frame="TEST")
    return data


def external_client(tmp_path, data, **options):
    path = tmp_path / "descriptors.json"
    path.write_text(json.dumps(data))
    return TestClient(create_app(Settings(environment="test", dataset_path=path, **options)))


@pytest.mark.parametrize("target_id", ["jezero", "gale", "elysium"])
def test_demo_preserves_science_and_geojson(client, target_id):
    response = client.post("/api/v1/analyze", json={"target_id": target_id})
    assert response.status_code == 200, response.text
    result = response.json()
    data = load_dataset(ROOT / "data/demo.json")
    target = next(row for row in data["mars"] if row["id"] == target_id)
    original = rank_candidates(target, data["earth"])
    assert result["synthetic"] is True
    assert result["data_mode"] == "demo"
    assert len(result["top_3"]) == len(result["geojson"]["features"]) == 3
    for row, expected, feature in zip(result["top_3"], original, result["geojson"]["features"]):
        assert row["site"]["id"] == expected["site"]["id"] == feature["id"]
        assert row["normalized_distance"] == expected["distance"]
        assert row["topographic"]["value"] == expected["distance"]
        assert row["thermal"]["value"] is None
        assert row["thermal"]["status"] == "unavailable"
        assert row["calibrated_similarity_percent"] is None
        assert feature["geometry"]["coordinates"] == [row["site"]["lon"], row["site"]["lat"]]
        assert feature["properties"]["synthetic"] is True


def test_weighted_distance_is_not_percentage(client):
    result = client.post("/api/v1/analyze", json={"weights": {"relief_m": 0, "slope_deg": 2, "roughness_m": 0}}).json()
    assert result["top_3"][0]["normalized_distance"] == pytest.approx(1 / 15)
    assert result["weights"] == {"relief_m": 0, "slope_deg": 1, "roughness_m": 0}
    assert result["calibrated_similarity_percent"] is None


@pytest.mark.parametrize("payload", [
    {"mission_type": "venus"}, {"mission_type": "moon"}, {"target_id": "absent"},
    {"weights": {"relief_m": -1}}, {"weights": {"relief_m": True}},
    {"weights": {"relief_m": "1"}}, {"weights": {"temperature": 1}},
    {"weights": {"relief_m": 0, "slope_deg": 0, "roughness_m": 0}},
    {"data_mode": "magic"}, {"data_mode": "live"}, {"source": "nasa_earthdata"},
    {"unexpected": 1}, {"target_id": "../secret"}, {"weights": None},
])
def test_invalid_request_422(client, payload):
    response = client.post("/api/v1/analyze", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"]["message"]


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e309"])
def test_nonfinite_payload_never_causes_500(client, number):
    response = client.post("/api/v1/analyze", content='{"weights":{"relief_m":' + number + '}}',
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 422


def test_no_silent_fallback(client):
    response = client.post("/api/v1/analyze", json={"data_mode": "external"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "data_not_configured"


@pytest.mark.parametrize("source", list(CONNECTORS))
def test_live_connectors_honestly_unavailable(client, source):
    response = client.post("/api/v1/analyze", json={"data_mode": "live", "source": source})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "source_unavailable"


def test_health_sources_and_openapi(client):
    assert client.get("/health").json()["live_sources_ready"] is False
    assert len(client.get("/api/v1/sources").json()["sources"]) == 6
    schema = client.get("/openapi.json").json()
    assert "AnalyzeRequest" in schema["components"]["schemas"]
    assert "503" in schema["paths"]["/api/v1/analyze"]["post"]["responses"]


@pytest.mark.parametrize("origin", ["*", "https://*.vercel.app", "http://myapp.vercel.app", "https://a.vercel.app/", "https://user:pass@a.vercel.app"])
def test_production_rejects_unsafe_origins(origin):
    with pytest.raises(ValueError):
        Settings(environment="production", cors_origins=(origin,))


def test_cors_exact_allowlist_including_errors():
    origin = "https://prometheus-example.vercel.app"
    with TestClient(create_app(Settings(environment="production", cors_origins=(origin,)))) as client:
        headers = {"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
        assert client.options("/api/v1/analyze", headers=headers).headers["access-control-allow-origin"] == origin
        for unknown in ("https://other.vercel.app", origin + ".evil.example"):
            response = client.options("/api/v1/analyze", headers={**headers, "Origin": unknown})
            assert response.status_code == 400
            assert "access-control-allow-origin" not in response.headers
        error = client.post("/api/v1/analyze", json={"data_mode": "external"}, headers={"Origin": origin})
        assert error.headers["access-control-allow-origin"] == origin


def test_empty_cors_does_not_allow_browser_origins(client):
    response = client.post("/api/v1/analyze", json={}, headers={"Origin": "https://any.vercel.app"})
    assert "access-control-allow-origin" not in response.headers


def test_body_limit(client):
    response = client.post("/api/v1/analyze", content=b" " * 16_385)
    assert response.status_code == 413


def test_external_top_three_moon_and_ties(tmp_path, external_data):
    external_data["targets"][0]["body"] = "moon"
    extra = copy.deepcopy(external_data["candidates"][0])
    extra.update(id="fourth", name="AAA tie")
    external_data["candidates"].append(extra)
    with external_client(tmp_path, external_data) as client:
        result = client.post("/api/v1/analyze", json={"data_mode": "external", "mission_type": "moon"}).json()
    assert result["synthetic"] is False
    assert len(result["top_3"]) == 3
    assert [row["rank"] for row in result["top_3"][:2]] == [1, 1]
    assert result["top_3"][0]["site"]["id"] == "fourth"


def test_missing_features_not_imputed(tmp_path, external_data):
    external_data["candidates"][0]["roughness_m"] = None
    with external_client(tmp_path, external_data) as client:
        invalid = client.post("/api/v1/analyze", json={"data_mode": "external"})
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "missing_weighted_features"
        result = client.post("/api/v1/analyze", json={"data_mode": "external", "weights": {"roughness_m": 0}}).json()
        site = next(row for row in result["top_3"] if row["site"]["id"] == "atacama")
        assert site["topographic"]["value"] is None
        assert "roughness_m" not in site["differences"]


@pytest.mark.parametrize("field,value", [("relief_m", -9999), ("slope_deg", 91), ("roughness_m", float("nan")), ("lat", 91), ("lon", "-1"), ("provenance", None)])
def test_invalid_source_is_503(tmp_path, external_data, field, value):
    external_data["candidates"][0][field] = value
    with external_client(tmp_path, external_data) as client:
        response = client.post("/api/v1/analyze", json={"data_mode": "external"})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "invalid_source_data"
        assert str(tmp_path) not in response.text


def test_thermal_requires_valid_comparable_measurements(tmp_path, external_data):
    target = external_data["targets"][0]
    measurement = dict(value=250, unit="K", quantity="surface_temperature", statistic="mean",
                       comparison_basis="TEST local noon same spatial support", quality="valid", provenance=target["provenance"])
    target["thermal"] = measurement
    external_data["candidates"][0]["thermal"] = {**measurement, "value": 275}
    external_data["candidates"][1]["thermal"] = {**measurement, "quantity": "brightness_temperature"}
    with external_client(tmp_path, external_data) as client:
        response = client.post("/api/v1/analyze", json={"data_mode": "external"})
        assert response.status_code == 200, response.text
        rows = {row["site"]["id"]: row for row in response.json()["top_3"]}
        assert rows["atacama"]["thermal"]["value"] == 25
        assert rows["cotopaxi"]["thermal"]["value"] is None
        assert rows["lanzarote"]["thermal"]["value"] is None
    target["thermal"]["value"] = -9999
    with external_client(tmp_path, external_data) as client:
        assert client.post("/api/v1/analyze", json={"data_mode": "external"}).status_code == 503


def test_dataset_size_and_mode_limits(tmp_path, external_data):
    with external_client(tmp_path, external_data, max_dataset_bytes=100) as client:
        assert client.post("/api/v1/analyze", json={"data_mode": "external"}).status_code == 503
    external_data["mode"] = "demo"
    with external_client(tmp_path, external_data) as client:
        assert client.post("/api/v1/analyze", json={"data_mode": "external"}).status_code == 503


def test_distance_can_exceed_one_without_clipping(external_data):
    for row in external_data["candidates"]:
        row["relief_m"] = 100_000
    result = analyze_descriptors(AnalyzeRequest(), DescriptorDataset.model_validate(external_data))
    assert result.top_3[0].normalized_distance > 1
    assert result.calibrated_similarity_percent is None


def test_request_timeout_is_503(monkeypatch):
    async def slow(self, request):
        await asyncio.sleep(1)

    monkeypatch.setattr(type(CONNECTORS["nasa_earthdata"]), "descriptors", slow)
    with TestClient(create_app(Settings(request_timeout_seconds=0.02))) as client:
        response = client.post("/api/v1/analyze", json={"data_mode": "live", "source": "nasa_earthdata"})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "request_timeout"


def test_legacy_external_dataset_still_supported(tmp_path):
    data = load_dataset(ROOT / "data/demo.json")
    data["mode"] = "external"
    for row in data["mars"] + data["earth"]:
        row.update(source="TEST ONLY", product_id="TEST", method="TEST",
                   resolution_m=30, window_m=1000, reference_frame="TEST")
    with external_client(tmp_path, data) as client:
        response = client.post("/api/v1/analyze", json={"data_mode": "external"})
        assert response.status_code == 200
        assert response.json()["target"]["provenance"]["source"] == "TEST ONLY"


@pytest.mark.parametrize("body", ["{", "null", "[]"])
def test_invalid_json_shape(client, body):
    assert client.post("/api/v1/analyze", content=body, headers={"Content-Type": "application/json"}).status_code == 422


def test_chunked_body_size_without_content_length():
    # Exercise the ASGI middleware directly, with multiple chunks and no length.
    app = create_app(Settings(max_body_bytes=10))
    messages = iter([{"type": "http.request", "body": b"a" * 6, "more_body": True},
                     {"type": "http.request", "body": b"b" * 6, "more_body": False}])
    sent = []
    async def receive():
        return next(messages)
    async def send(message):
        sent.append(message)
    asyncio.run(app({"type": "http", "method": "POST", "path": "/api/v1/analyze",
                     "headers": [], "query_string": b"", "scheme": "http"}, receive, send))
    assert sent[0]["status"] == 413
