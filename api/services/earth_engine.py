import asyncio
from collections import OrderedDict, deque
from datetime import datetime, timezone
import json
import math
import sys
import time

from pydantic import ValidationError

from api.config import ROOT, Settings
from api.connectors.gee_worker import DEM_ID, METHOD, SCALE_M, THERMAL_ID
from api.errors import AnalysisError
from api.gee_schemas import LocationFeature, LocationProperties, LocationsResponse
from api.schemas import EarthEngineQuery, EarthLocation, PointGeometry, Provenance, Region, ThermalMeasurement


def query_sites(query: EarthEngineQuery) -> list[EarthLocation]:
    if query.sites:
        return query.sites
    west, south, east, north = query.bbox
    return [EarthLocation(id=f"grid-{row}-{column}", name=f"Centro de muestreo {row + 1}-{column + 1}",
                          lon=west + (column + .5) * (east - west) / 3,
                          lat=south + (row + .5) * (north - south) / 3)
            for row in range(3) for column in range(3)]


def number(value, *, minimum=None):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Valor de proveedor no finito o no numérico")
    if minimum is not None and value < minimum:
        raise ValueError("Valor de proveedor fuera de rango")
    return value


def count(value):
    if value is None:
        return 0
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 40_000 or int(value) != value):
        raise ValueError("Recuento de proveedor inválido")
    return int(value)


def convert_result(raw, query: EarthEngineQuery) -> LocationsResponse:
    sites = query_sites(query)
    if not isinstance(raw, dict) or raw.get("type") != "FeatureCollection" or not isinstance(raw.get("features"), list):
        raise ValueError("Respuesta GEE inválida")
    rows = raw["features"]
    if len(rows) != len(sites):
        raise ValueError("Respuesta GEE incompleta")
    features = []
    for site, feature in zip(sites, rows):
        data = feature["properties"]
        if data["id"] != site.id:
            raise ValueError("IDs de GEE no coinciden")
        stats = data["stats"]
        valid = count(stats.get("elevation_count"))
        total = count(data.get("total_pixels"))
        if valid > total:
            raise ValueError("Cobertura GEE inválida")
        coverage = valid / total if total else None
        low, high = number(stats.get("elevation_min")), number(stats.get("elevation_max"))
        if low is not None and high is not None and high < low:
            raise ValueError("Rango de elevación inválido")
        mean = number(stats.get("elevation_mean"))
        slope = number(stats.get("slope_mean"), minimum=0)
        roughness = number(data.get("roughness_m"), minimum=0)
        enough = valid >= 4 and coverage is not None and coverage >= .8
        messages = []
        if not enough:
            messages.append("Topografía no disponible: cobertura válida inferior al 80% o menos de cuatro píxeles. No se imputan datos.")
        provenance = Provenance(source="NASA via Google Earth Engine", product_id=DEM_ID,
                                method=METHOD, resolution_m=SCALE_M, window_m=query.window_m,
                                reference_frame=f"WGS84 / {data['crs']}; vertical EGM96")
        thermal, thermal_pixels, image_count = None, 0, 0
        first_ms, last_ms = None, None
        if query.start_date and data.get("thermal"):
            block = data["thermal"]
            image_count = count(block.get("images"))
            thermal_pixels = count(block["stats"].get("lst_count"))
            value = number(block["stats"].get("lst_mean"), minimum=0)
            first_ms, last_ms = number(block.get("first_ms"), minimum=0), number(block.get("last_ms"), minimum=0)
            if image_count and thermal_pixels and value is not None and value > 0:
                thermal = ThermalMeasurement(value=value, unit="K", quantity="surface_temperature", statistic="mean",
                    quality="valid", comparison_basis=f"MOD11A2-day-QC0; {query.start_date}/{query.end_date}; pixel-composite-then-spatial-mean; window={query.window_m}m",
                    provenance=Provenance(source="NASA MODIS via Google Earth Engine", product_id=THERMAL_ID,
                        method="QC_Day=0; DN*0.02; mean of available 8-day composites, then spatial mean",
                        resolution_m=1000, window_m=query.window_m, reference_frame="MODIS sinusoidal SR-ORG:6974"))
        if thermal is None:
            messages.append("Temperatura no disponible: periodo no solicitado o sin píxeles válidos tras QA.")
        region = Region(id=site.id, name=site.name, body="earth", lat=site.lat, lon=site.lon,
                        relief_m=high - low if enough and low is not None and high is not None else None,
                        slope_deg=slope if enough else None, roughness_m=roughness if enough else None,
                        provenance=provenance, thermal=thermal)
        features.append(LocationFeature(id=site.id, geometry=PointGeometry(coordinates=(site.lon, site.lat)),
            properties=LocationProperties(region=region, elevation_mean_m=mean if enough else None,
                topography_valid_pixels=valid, topography_coverage_fraction=coverage,
                thermal_valid_pixels=thermal_pixels, thermal_image_count=image_count,
                thermal_first_composite_ms=first_ms, thermal_last_composite_ms=last_ms, messages=messages)))
    return LocationsResponse(retrieved_at=datetime.now(timezone.utc).isoformat(), features=features,
        messages=["Muestreo terrestre exploratorio; no certifica seguridad, accesibilidad ni transitabilidad de robots.",
                  "NASADEM es histórico; MODIS es un compuesto de ocho días, no clima en tiempo real.",
                  "Una bbox genera nueve centros: no es búsqueda global ni cobertura exhaustiva; las ventanas pueden superar sus bordes.",
                  "No se obtienen datos lunares/marcianos ni mineralogía de estos productos."])


async def run_json_process(command, payload, timeout):
    process = await asyncio.create_subprocess_exec(*command, cwd=ROOT, stdin=asyncio.subprocess.PIPE,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(timeout):
            process.stdin.write(json.dumps(payload).encode())
            await process.stdin.drain()
            process.stdin.close()
            raw = bytearray()
            while chunk := await process.stdout.read(8192):
                raw.extend(chunk)
                if len(raw) > 65_536:
                    raise AnalysisError(503, "gee_response_limit", "La respuesta reducida supera el límite de 64 KiB.")
            await process.wait()
            if process.returncode != 0:
                raise AnalysisError(503, "gee_unavailable", "Earth Engine no disponible: revise acceso del proyecto, credenciales, cuotas y productos.")
            return json.loads(raw)
    except TimeoutError as exc:
        raise AnalysisError(503, "gee_timeout", "Earth Engine agotó 25 s; reduzca el número de sitios o vuelva a intentar más tarde.") from exc
    finally:
        # Timeout or HTTP cancellation must not leave a billed computation client running.
        if process.returncode is None:
            process.kill()
        await process.wait()


class EarthEngineService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = asyncio.Lock()
        self.cache = OrderedDict()
        self.calls = deque()

    def status(self):
        configured = bool(self.settings.gee_enabled and self.settings.gee_project_id and self.settings.gee_credentials_path)
        return {"source": "google_earth_engine", "name": "Google Earth Engine",
                "status": "configured_unverified" if configured else "not_configured",
                "implemented": True, "live_access_verified": False,
                "message": "Requiere proyecto registrado y Secret File en Render; configuración no demuestra acceso.",
                "products": [DEM_ID, THERMAL_ID]}

    async def sample(self, query: EarthEngineQuery):
        if self.status()["status"] == "not_configured":
            raise AnalysisError(503, "gee_not_configured", "Configure GEE_ENABLED, GEE_PROJECT_ID y GOOGLE_APPLICATION_CREDENTIALS en Render. La demo sigue disponible.")
        key = query.model_dump_json()
        now = time.monotonic()
        if key in self.cache and self.cache[key][0] > now:
            self.cache.move_to_end(key)
            result = self.cache[key][1].model_copy(deep=True)
            result.cached = True
            return result
        if self.lock.locked():
            raise AnalysisError(503, "gee_busy", "Ya hay una consulta Earth Engine en curso; vuelva a intentar más tarde.")
        async with self.lock:
            while self.calls and self.calls[0] <= now - 60:
                self.calls.popleft()
            if len(self.calls) >= 6:
                raise AnalysisError(429, "gee_rate_limit", "Máximo seis consultas nuevas por minuto y proceso.")
            self.calls.append(now)
            payload = {"project_id": self.settings.gee_project_id,
                       "credentials_path": str(self.settings.gee_credentials_path),
                       "query": {**query.model_dump(mode="json"), "sites": [site.model_dump() for site in query_sites(query)]}}
            try:
                raw = await run_json_process([sys.executable, "-m", "api.connectors.gee_worker"], payload,
                                             self.settings.gee_timeout_seconds)
                result = convert_result(raw, query)
            except (ValueError, KeyError, TypeError, AttributeError, IndexError, OSError, ValidationError) as exc:
                raise AnalysisError(503, "invalid_gee_data", "Earth Engine no entregó descriptores válidos; no se sustituyen con datos demo.") from exc
            self.cache[key] = (time.monotonic() + 900, result.model_copy(deep=True))
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return result
