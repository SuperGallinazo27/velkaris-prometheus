"""Reusable bounded JSON transport for future product-specific adapters.

Only operator-selected HTTPS hosts/endpoints are permitted. No user URL field
exists in the public API. Never use this transport to retrieve raster assets.
"""

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from api.errors import AnalysisError


@dataclass(frozen=True)
class QueryBudget:
    max_response_bytes: int = 1_048_576
    timeout_seconds: float = 8.0
    max_items: int = 20
    max_pixels: int = 262_144

    def __post_init__(self):
        if min(self.max_response_bytes, self.timeout_seconds, self.max_items, self.max_pixels) <= 0:
            raise ValueError("Los límites deben ser positivos.")


async def bounded_json(client: httpx.AsyncClient, url: str, *, allowed_hosts: set[str],
                       payload: dict, budget: QueryBudget = QueryBudget()) -> dict:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise AnalysisError(503, "source_not_allowed", "Endpoint de fuente no permitido.")
    try:
        async with asyncio.timeout(budget.timeout_seconds):
            async with client.stream("POST", url, json=payload, follow_redirects=False,
                                     headers={"Accept-Encoding": "identity"},
                                     timeout=budget.timeout_seconds) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("No se aceptan respuestas comprimidas sin presupuesto de expansión.")
                if "json" not in response.headers.get("content-type", "").lower():
                    raise ValueError("Solo respuestas JSON reducidas.")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16_384):
                    if len(body) + len(chunk) > budget.max_response_bytes:
                        raise ValueError("Respuesta demasiado grande.")
                    body.extend(chunk)
                data = json.loads(body, parse_constant=reject_nonfinite)
                if not isinstance(data, dict):
                    raise ValueError("Se esperaba un objeto JSON.")
                return data
    except (httpx.HTTPError, TimeoutError, ValueError, RecursionError) as exc:
        raise AnalysisError(503, "upstream_unavailable", "Proveedor no disponible, respuesta inválida o límite de consulta excedido.") from exc


def reject_nonfinite(value):
    raise ValueError("JSON no finito.")


async def search_stac_page(client: httpx.AsyncClient, url: str, *, allowed_hosts: set[str],
                           collection: str, bbox: tuple[float, float, float, float],
                           datetime_range: str, budget: QueryBudget = QueryBudget()) -> dict:
    """One bounded metadata page. Never follows pagination or asset links."""
    data = await bounded_json(client, url, allowed_hosts=allowed_hosts, budget=budget,
                              payload={"collections": [collection], "bbox": list(bbox),
                                       "datetime": datetime_range, "limit": budget.max_items})
    if (data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list)
            or len(data["features"]) > budget.max_items):
        raise AnalysisError(503, "invalid_stac_page", "Página STAC inválida o con demasiados elementos.")
    return data
