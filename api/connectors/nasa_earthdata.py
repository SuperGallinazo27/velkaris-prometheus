from .base import SourceConnector

connector = SourceConnector(
    "nasa_earthdata", "NASA EarthData", "Earthdata Login para Harmony y productos protegidos; depende del servicio.",
    "CMR/STAC para descubrimiento; Harmony para subconjuntos soportados; COG autorizado por ventanas.",
)
