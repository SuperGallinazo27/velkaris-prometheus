import asyncio

import httpx
import pytest

from api.connectors.transport import QueryBudget, bounded_json, search_stac_page
from api.errors import AnalysisError

URL = "https://catalog.example.test/search"


def run_transport(handler, **options):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await bounded_json(client, URL, allowed_hosts={"catalog.example.test"}, payload={}, **options)
    return asyncio.run(run())


@pytest.mark.parametrize("response", [
    httpx.Response(401), httpx.Response(429), httpx.Response(500),
    httpx.Response(302, headers={"Location": "https://evil.example"}),
    httpx.Response(200, content="raster", headers={"content-type": "image/tiff"}),
    httpx.Response(200, content="{", headers={"content-type": "application/json"}),
    httpx.Response(200, json=[]),
])
def test_provider_failures_are_503(response):
    with pytest.raises(AnalysisError) as error:
        run_transport(lambda request: response)
    assert error.value.status_code == 503


def test_bounded_bytes_without_content_length():
    with pytest.raises(AnalysisError):
        run_transport(lambda request: httpx.Response(200, json={"data": "x" * 500}),
                      budget=QueryBudget(max_response_bytes=100))


def test_deadline():
    async def handler(request):
        await asyncio.sleep(1)
    with pytest.raises(AnalysisError):
        run_transport(handler, budget=QueryBudget(timeout_seconds=0.01))


def test_stac_only_one_page_no_asset_fetch():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"type": "FeatureCollection", "features": [],
                                        "links": [{"rel": "next", "href": "https://evil.example"}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await search_stac_page(client, URL, allowed_hosts={"catalog.example.test"},
                                           collection="test", bbox=(0, 0, 1, 1), datetime_range="2020-01-01/2020-02-01")
    assert asyncio.run(run())["features"] == []
    assert len(calls) == 1


def test_no_unapproved_endpoint_requests():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("Unexpected network call"))) as client:
            await bounded_json(client, "http://127.0.0.1/private", allowed_hosts={"catalog.example.test"}, payload={})
    with pytest.raises(AnalysisError):
        asyncio.run(run())


def test_stac_item_limit():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"type": "FeatureCollection", "features": [{}] * 21}))) as client:
            return await search_stac_page(client, URL, allowed_hosts={"catalog.example.test"},
                                           collection="test", bbox=(0, 0, 1, 1), datetime_range="2020-01-01/2020-02-01")
    with pytest.raises(AnalysisError):
        asyncio.run(run())
