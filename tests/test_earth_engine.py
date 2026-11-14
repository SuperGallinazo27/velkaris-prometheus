import asyncio
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.connectors.gee_worker import METHOD, build_query
from api.errors import AnalysisError
from api.main import create_app
from api.schemas import EarthEngineQuery
from api.services import earth_engine as service_module
from api.services.earth_engine import EarthEngineService, convert_result, count, query_sites, run_json_process

SITES = [dict(id=f"test-{index}", name=f"TEST ONLY {index}", lat=-24.5 + index * .01, lon=-69.2) for index in range(3)]
QUERY = {"sites": SITES, "window_m": 3000, "start_date": "2024-01-01", "end_date": "2024-01-17"}


def fixture_result(query):
    # Fabricated test transport response ONLY. Never a shipped data source.
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {
        "id": site.id, "crs": "EPSG:32719", "total_pixels": 10000,
        "stats": {"elevation_min": 100, "elevation_max": 300, "elevation_mean": 200,
                  "elevation_count": 9500, "slope_mean": 5, "slope_count": 9400},
        "roughness_m": 12,
        "thermal": {"stats": {"lst_mean": 280, "lst_count": 8}, "images": 2,
                    "first_ms": 1704067200000, "last_ms": 1704758400000}
    }} for site in query_sites(query)]}


def enabled_settings(**kwargs):
    return Settings(gee_enabled=True, gee_project_id="TEST_PROJECT", gee_credentials_path=Path("/TEST_ONLY.json"), **kwargs)


@pytest.fixture
def mock_worker(monkeypatch):
    calls = []
    async def run(command, payload, timeout):
        calls.append(payload)
        query = EarthEngineQuery(**{**payload["query"], "bbox": None})
        return fixture_result(query)
    monkeypatch.setattr(service_module, "run_json_process", run)
    return calls


def test_not_configured_does_not_fake_data_or_break_demo():
    with TestClient(create_app(Settings())) as client:
        response = client.post("/api/v1/earth-engine/locations", json=QUERY)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "gee_not_configured"
        assert client.post("/api/v1/analyze", json={}).status_code == 200
        source = client.get("/api/v1/sources").json()["sources"][-1]
        assert source["implemented"] is True
        assert source["live_access_verified"] is False


@pytest.mark.parametrize("payload", [
    {}, {"sites": []}, {"sites": SITES * 4}, {"sites": [SITES[0], SITES[0]]},
    {"sites": SITES, "bbox": [-70, -25, -69, -24]},
    {"bbox": [-70, -25, -68, -24]}, {"bbox": [-70, -25, -71, -24]},
    {"bbox": [179.9, -25, -179.9, -24]}, {"bbox": [-70, 70, -69, 71]},
    {"sites": [{**SITES[0], "lat": "-24"}]},
    {"sites": SITES, "window_m": 5001}, {"sites": SITES, "window_m": True},
    {"sites": SITES, "start_date": "2024-01-01"},
    {**QUERY, "end_date": "2024-02-15"}, {**QUERY, "end_date": "2023-01-01"},
    {**QUERY, "window_m": 1000}, {"sites": SITES, "asset_url": "https://example.com"},
])
def test_queries_are_bounded(payload):
    with TestClient(create_app(Settings())) as client:
        assert client.post("/api/v1/earth-engine/locations", json=payload).status_code == 422


def test_geolocation_geojson_provenance_and_cache(mock_worker):
    with TestClient(create_app(enabled_settings())) as client:
        first = client.post("/api/v1/earth-engine/locations", json=QUERY)
        assert first.status_code == 200, first.text
        result = first.json()
        assert result["synthetic"] is False and result["data_mode"] == "live"
        feature = result["features"][0]
        assert feature["geometry"]["coordinates"] == [-69.2, -24.5]
        region = feature["properties"]["region"]
        assert region["relief_m"] == 200
        assert region["roughness_m"] == 12
        assert region["thermal"]["value"] == 280
        assert region["thermal"]["provenance"]["resolution_m"] == 1000
        assert region["provenance"]["method"] == METHOD
        second = client.post("/api/v1/earth-engine/locations", json=QUERY).json()
        assert second["cached"] is True
        assert second["retrieved_at"] == result["retrieved_at"]
        assert len(mock_worker) == 1


def test_bbox_samples_nine_centers(mock_worker):
    with TestClient(create_app(enabled_settings())) as client:
        result = client.post("/api/v1/earth-engine/locations", json={"bbox": [-70, -25, -69, -24]}).json()
        assert len(result["features"]) == 9
        assert all(-70 < feature["geometry"]["coordinates"][0] < -69 for feature in result["features"])


def test_nodata_remains_null():
    query = EarthEngineQuery(**QUERY)
    raw = fixture_result(query)
    raw["features"][0]["properties"].update(stats={"elevation_count": 0}, roughness_m=None, thermal={"stats": {}, "images": 0})
    region = convert_result(raw, query).features[0].properties.region
    assert region.relief_m is region.slope_deg is region.roughness_m is region.thermal is None


def test_counts_accept_integral_json_numbers_but_not_fractional():
    assert count(42.0) == 42
    with pytest.raises(ValueError):
        count(42.5)


def test_http_cancellation_terminates_worker(monkeypatch):
    real_spawn = asyncio.create_subprocess_exec
    processes = []
    async def spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async def run():
        task = asyncio.create_task(run_json_process([sys.executable, "-c", "import time; time.sleep(30)"], {}, 10))
        for _ in range(100):
            if processes:
                break
            await asyncio.sleep(.01)
        assert processes
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
    asyncio.run(run())


@pytest.mark.parametrize("key,value", [("elevation_min", float("nan")), ("elevation_count", True), ("elevation_count", 10001), ("slope_mean", 91)])
def test_invalid_upstream_values_rejected(key, value):
    query = EarthEngineQuery(**QUERY)
    raw = fixture_result(query)
    raw["features"][0]["properties"]["stats"][key] = value
    with pytest.raises(ValueError):
        convert_result(raw, query)


def test_busy_and_rate_limit(mock_worker):
    async def run():
        service = EarthEngineService(enabled_settings())
        async with service.lock:
            with pytest.raises(AnalysisError) as error:
                await service.sample(EarthEngineQuery(**QUERY))
            assert error.value.code == "gee_busy"
        for index in range(6):
            query = EarthEngineQuery(sites=[{**SITES[0], "lat": -24 + index * .01}])
            await service.sample(query)
        with pytest.raises(AnalysisError) as error:
            await service.sample(EarthEngineQuery(sites=[{**SITES[0], "lat": -23}]))
        assert error.value.status_code == 429
    asyncio.run(run())


def test_worker_timeout_and_output_cap():
    async def run():
        for script, code in [("import time; time.sleep(30)", "gee_timeout"),
                             ("print('x'*70000)", "gee_response_limit")]:
            with pytest.raises(AnalysisError) as error:
                await run_json_process([sys.executable, "-c", script], {}, .5)
            assert error.value.code == code
    asyncio.run(run())


def test_real_worker_missing_credentials_fails_without_leaking_paths():
    async def run():
        with pytest.raises(AnalysisError) as error:
            await run_json_process([sys.executable, "-m", "api.connectors.gee_worker"],
                {"credentials_path": "/THIS_IS_NOT_A_SECRET_BUT_MUST_NOT_LEAK.json", "project_id": "TEST", "query": QUERY}, 10)
        assert error.value.code == "gee_unavailable"
        assert "MUST_NOT_LEAK" not in str(error.value)
    asyncio.run(run())


def test_official_sdk_serializes_computation_without_network(monkeypatch):
    import ee
    from ee.apitestcase import GetAlgorithms
    ee.Reset()
    monkeypatch.setattr(ee.data, "_install_cloud_api_resource", lambda: None)
    monkeypatch.setattr(ee.data, "getAlgorithms", GetAlgorithms)
    # A graph build must never evaluate/download data during serialization.
    monkeypatch.setattr(ee.data, "computeValue", lambda _: pytest.fail("Unexpected live request"))
    ee.Initialize(None, "", project="TEST")
    try:
        graph = build_query(ee, SITES, 3000, "2024-01-01", "2024-01-17")
        encoded = graph.serialize()
        assert "NASA/NASADEM_HGT/001" in encoded and "MODIS/061/MOD11A2" in encoded
        assert "Reducer.linearRegression" in encoded and "Terrain.slope" in encoded
        assert "QC_Day" in encoded and "0.02" in encoded
        assert "Image.reduceRegion" in encoded
        assert "40000" in encoded and '"bestEffort"' in encoded
    finally:
        ee.Reset()


def catalog_file(tmp_path, **overrides):
    target = dict(id="scientific-target", name="TEST ONLY", body="mars", relief_m=200, slope_deg=5, roughness_m=12,
                  provenance=dict(source="TEST ONLY", product_id="TEST", method=METHOD,
                                  resolution_m=30, window_m=3000, reference_frame="TEST MARS"))
    target.update(overrides)
    path = tmp_path / "targets.json"
    path.write_text(json.dumps({"schema_version": 1, "mode": "external", "targets": [target]}))
    return path


def test_live_analysis_uses_only_external_planetary_target(tmp_path, mock_worker):
    settings = enabled_settings(targets_path=catalog_file(tmp_path))
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/analyze", json={"target_id": "scientific-target", "data_mode": "live",
                        "source": "google_earth_engine", "earth_engine": QUERY})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["data_mode"] == "live" and result["source"] == "google_earth_engine"
        assert result["synthetic"] is False
        assert len(result["top_3"]) == 3
        assert all(row["normalized_distance"] == 0 and row["thermal"]["value"] is None for row in result["top_3"])


def test_incompatible_target_rejected_before_gee_call(tmp_path, mock_worker):
    path = catalog_file(tmp_path)
    catalog = json.loads(path.read_text())
    catalog["targets"][0]["provenance"]["method"] = "Different method"
    path.write_text(json.dumps(catalog))
    with TestClient(create_app(enabled_settings(targets_path=path))) as client:
        result = client.post("/api/v1/analyze", json={"target_id": "scientific-target", "data_mode": "live",
                            "source": "google_earth_engine", "earth_engine": QUERY})
        assert result.status_code == 422
        assert result.json()["detail"]["code"] == "incompatible_target_method"
        assert mock_worker == []


def test_live_never_uses_demo_target(mock_worker):
    with TestClient(create_app(enabled_settings())) as client:
        response = client.post("/api/v1/analyze", json={"data_mode": "live", "source": "google_earth_engine", "earth_engine": QUERY})
        assert response.status_code == 503
        assert mock_worker == []
