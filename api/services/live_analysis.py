import asyncio
import json

from api.connectors.gee_worker import METHOD, SCALE_M
from api.errors import AnalysisError
from api.gee_schemas import TargetCatalog
from api.schemas import DescriptorDataset
from api.services.analyze import analyze_descriptors
from api.services.datasets import load_descriptors
from api.services.earth_engine import query_sites


def load_targets(settings):
    if settings.targets_path is None:
        # Existing external dataset is supported; never use the synthetic demo.
        return load_descriptors("external", settings).targets
    try:
        with settings.targets_path.open("rb") as source:
            raw = source.read(settings.max_dataset_bytes + 1)
        if len(raw) > settings.max_dataset_bytes:
            raise ValueError("Catálogo demasiado grande")
        return TargetCatalog.model_validate(json.loads(raw)).targets
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise AnalysisError(503, "invalid_target_catalog", "Catálogo planetario inválido: revise tamaño, datos finitos y procedencia. No se usan objetivos demo con Tierra real.") from exc


async def analyze_earth_engine(payload, settings, service):
    sites = query_sites(payload.earth_engine)
    if len(sites) < 3:
        raise AnalysisError(422, "too_few_candidates", "El ranking Top 3 requiere al menos tres sitios o una bbox.")
    targets = await asyncio.to_thread(load_targets, settings)
    target = next((row for row in targets if row.id == payload.target_id and row.body == payload.mission_type), None)
    if target is None:
        raise AnalysisError(422, "unknown_target", "Objetivo no disponible en el catálogo planetario externo.")
    provenance = target.provenance
    if (provenance.method != METHOD or provenance.resolution_m != SCALE_M
            or provenance.window_m != payload.earth_engine.window_m):
        raise AnalysisError(422, "incompatible_target_method", "El objetivo debe usar prometheus_topography_plane_v1, resolución 30 m y la misma ventana. Recalcule los descriptores; no cambie solo la etiqueta.")
    if target.id in {site.id for site in sites}:
        raise AnalysisError(422, "duplicate_target_id", "El ID objetivo no puede coincidir con un candidato.")
    # Fail before consuming GEE quota when the target lacks an active descriptor.
    for key, weight in payload.weights.model_dump().items():
        if weight > 0 and getattr(target, key) is None:
            raise AnalysisError(422, "missing_weighted_features", f"El objetivo no contiene {key} con peso positivo.")
    locations = await service.sample(payload.earth_engine)
    dataset = DescriptorDataset(mode="external", description="Objetivo planetario externo y candidatos terrestres calculados en Google Earth Engine.",
                                targets=[target], candidates=[feature.properties.region for feature in locations.features])
    response = analyze_descriptors(payload, dataset)
    response.data_mode = "live"
    response.source = "google_earth_engine"
    for feature in response.geojson.features:
        feature.properties.data_mode = "live"
    response.messages.extend(locations.messages)
    for feature in locations.features:
        response.messages.extend(f"{feature.id}: {message}" for message in feature.properties.messages)
    return response
