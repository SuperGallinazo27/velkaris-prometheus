from api.errors import AnalysisError
from api.schemas import (
    AnalyzeRequest, AnalyzeResponse, DescriptorDataset, Feature, FeatureCollection,
    FeatureProperties, Indicator, PointGeometry, RankedSite, Region, Weights,
)
from prometheus.analysis import compare_topography


def thermal_indicator(target: Region, site: Region) -> Indicator:
    left, right = target.thermal, site.thermal
    if not left or not right:
        return Indicator(value=None, unit="K", status="unavailable",
                         message="Faltan mediciones térmicas válidas y con procedencia en una o ambas regiones.")
    if (left.quantity, left.statistic, left.comparison_basis) != (right.quantity, right.statistic, right.comparison_basis):
        return Indicator(value=None, unit="K", status="unavailable",
                         message="Las magnitudes o bases de comparación térmica no coinciden.")
    return Indicator(value=abs(left.value - right.value), unit="K", status="available",
                     message="Diferencia absoluta en kelvin; comparabilidad declarada por la fuente, no validada científicamente. No participa en el ranking.")


def analyze_descriptors(request: AnalyzeRequest, dataset: DescriptorDataset) -> AnalyzeResponse:
    target = next((row for row in dataset.targets if row.id == request.target_id and row.body == request.mission_type), None)
    if target is None:
        raise AnalysisError(422, "unknown_target", "target_id no está disponible para mission_type en esta fuente. La demo solo contiene Marte.")
    weights = request.weights.model_dump()
    total = sum(weights.values())
    normalized_weights = Weights(**{key: value / total for key, value in weights.items()})
    ranked = []
    for site in dataset.candidates:
        try:
            distance, differences, baseline = compare_topography(target.model_dump(), site.model_dump(), weights)
        except ValueError as exc:
            raise AnalysisError(422, "missing_weighted_features", f"{site.id}: {exc}. No se imputan valores ni se eliminan candidatos silenciosamente.") from exc
        ranked.append(RankedSite(
            rank=1, site=site, normalized_distance=distance, differences=differences,
            topographic=Indicator(value=baseline, unit="normalized_distance",
                                  status="available" if baseline is not None else "unavailable",
                                  message="Distancia original con pesos iguales de relieve, pendiente y rugosidad." if baseline is not None else "Faltan descriptores para el indicador topográfico completo; ranking solo con los pesos solicitados."),
            thermal=thermal_indicator(target, site),
        ))
    ranked.sort(key=lambda row: (row.normalized_distance, row.site.name, row.site.id))
    for index, row in enumerate(ranked):
        row.rank = ranked[index - 1].rank if index and row.normalized_distance == ranked[index - 1].normalized_distance else index + 1
    top = ranked[:3]
    synthetic = dataset.mode == "demo"
    messages = [dataset.description,
                "Distancia exploratoria: menor es más próximo; no es porcentaje ni probabilidad y puede superar 1.",
                "Escalas ilustrativas: relieve 1000 m, pendiente 15°, rugosidad 100 m. No hay calibración científica.",
                "No se evalúa mineralogía; ningún producto radar/óptico se interpreta automáticamente como temperatura o composición."]
    messages.append("DEMO: todos los descriptores son sintéticos; los puntos del mapa son orientativos." if synthetic else
                    "Descriptores externos: la procedencia declarada no demuestra autenticidad ni comparabilidad científica; verificar métodos, resolución y ventanas.")
    return AnalyzeResponse(
        mission_type=request.mission_type, data_mode=dataset.mode, synthetic=synthetic,
        target=target, weights=normalized_weights, top_3=top, messages=messages,
        geojson=FeatureCollection(features=[Feature(
            id=row.site.id,
            geometry=PointGeometry(coordinates=(row.site.lon, row.site.lat)),
            properties=FeatureProperties(name=row.site.name, rank=row.rank,
                                         normalized_distance=row.normalized_distance,
                                         data_mode=dataset.mode, synthetic=synthetic),
        ) for row in top]),
    )
