"""El Niño / La Niña indices from NOAA's Climate Prediction Center, relayed with a memory cache.

The browser cannot read NOAA's text tables (no CORS headers), so the frontend asks this service.
The upstream URLs are fixed: nothing in a request chooses what is fetched. The tables change once
a week at most, so one successful read is reused for hours and a failed one is not retried at once.
"""

import asyncio
import re
import time
from datetime import date, datetime, timezone

import httpx

from api.errors import AnalysisError

ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
WEEKLY_URL = "https://www.cpc.ncep.noaa.gov/data/indices/wksst9120.for"
SOURCE = {"name": "NOAA Climate Prediction Center", "oni": ONI_URL, "weekly": WEEKLY_URL}
CACHE_SECONDS = 6 * 3600.0
RETRY_SECONDS = 300.0
TIMEOUT_SECONDS = 15.0
MAX_BYTES = 2_000_000
# Five overlapping seasons above the threshold are what NOAA calls an episode.
RECENT_SEASONS = 5
MONTHS = {name: number + 1 for number, name in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
ONI_LINE = re.compile(r"^\s*([A-Z]{3})\s+(\d{4})\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)\s*$")
# Negative anomalies touch the temperature before them: "23.4-0.4".
WEEK_LINE = re.compile(r"^\s*(\d{2})([A-Z]{3})(\d{4})" + r"\s+(\d+\.\d)\s*(-?\d+\.\d)" * 4 + r"\s*$")


def parse_oni(text: str) -> list[dict]:
    rows = [{"season": match[1], "year": int(match[2]), "oni": float(match[4])}
            for match in map(ONI_LINE.match, text.splitlines()) if match]
    if len(rows) < RECENT_SEASONS:
        raise ValueError("ONI table without recognisable rows")
    return rows[-RECENT_SEASONS:]


def parse_weekly(text: str) -> dict:
    matches = [match for match in map(WEEK_LINE.match, text.splitlines()) if match and match[2] in MONTHS]
    if not matches:
        raise ValueError("weekly table without recognisable rows")
    last = matches[-1]
    return {
        "week": date(int(last[3]), MONTHS[last[2]], int(last[1])).isoformat(),
        "nino12": {"sst": float(last[4]), "anomaly": float(last[5])},
        "nino34": {"sst": float(last[8]), "anomaly": float(last[9])},
    }


class EnsoService:
    """One reading shared by every request. `transport` lets tests answer instead of NOAA."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, clock=time.monotonic):
        self._transport, self._clock = transport, clock
        self._lock = asyncio.Lock()
        self._value: dict | None = None
        self._read_at = 0.0
        self._failed_at: float | None = None

    async def _text(self, client: httpx.AsyncClient, url: str) -> str:
        response = await client.get(url)
        response.raise_for_status()
        if len(response.content) > MAX_BYTES:
            raise ValueError("upstream table too large")
        return response.content.decode("ascii", errors="replace")

    async def _read(self) -> dict:
        async with httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT_SECONDS, follow_redirects=False,
                                     headers={"User-Agent": "prometheus-api/1.0"}) as client:
            oni, weekly = await asyncio.gather(self._text(client, ONI_URL), self._text(client, WEEKLY_URL),
                                               return_exceptions=True)
        if isinstance(oni, BaseException):
            raise oni
        # The weekly table is a complement: without it the index is still worth serving.
        try:
            week = None if isinstance(weekly, BaseException) else parse_weekly(weekly)
        except ValueError:
            week = None
        return {
            "fetched_at": datetime.now(timezone.utc).date().isoformat(),
            "recent": parse_oni(oni),
            "weekly": week,
            "source": SOURCE,
        }

    async def reading(self) -> dict:
        async with self._lock:
            now = self._clock()
            if self._value is not None and now - self._read_at < CACHE_SECONDS:
                return self._value
            if self._failed_at is None or now - self._failed_at >= RETRY_SECONDS:
                try:
                    self._value, self._read_at, self._failed_at = await self._read(), now, None
                    return self._value
                except (httpx.HTTPError, ValueError):
                    self._failed_at = now
            if self._value is not None:
                # An old reading still says more than an error; its date travels with it.
                return self._value
            raise AnalysisError(503, "enso_unavailable",
                                "No se pudo leer el índice de El Niño de NOAA. Inténtelo de nuevo en unos minutos.")
