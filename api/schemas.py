"""Strict request, reduced-descriptor, response and GeoJSON contracts."""

from typing import Annotated, Literal
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator

Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Nonnegative = Annotated[Finite, Field(ge=0, le=1e9)]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=500)]
Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")]
Mission = Literal["mars", "moon"]
Source = Literal["nasa_earthdata", "esa_copernicus", "jaxa_gportal", "csa_radarsat", "nasa_pds_diviner", "google_earth_engine"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Weights(StrictModel):
    relief_m: Nonnegative = 1.0
    slope_deg: Nonnegative = 1.0
    roughness_m: Nonnegative = 1.0

    @model_validator(mode="after")
    def nonzero(self):
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("Al menos un peso debe ser mayor que cero.")
        return self


class EarthLocation(StrictModel):
    id: Identifier
    name: Text
    lat: Annotated[Finite, Field(ge=-55.9, le=59.9)]
    lon: Annotated[Finite, Field(ge=-179.9, le=179.9)]


class EarthEngineQuery(StrictModel):
    sites: Annotated[list[EarthLocation], Field(min_length=1, max_length=9)] | None = None
    bbox: tuple[Finite, Finite, Finite, Finite] | None = None
    window_m: Annotated[int, Field(strict=True, ge=1000, le=5000)] = 3000
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def bounded_query(self):
        if (self.sites is None) == (self.bbox is None):
            raise ValueError("Indique sites o bbox, exclusivamente uno.")
        if self.sites and len({site.id for site in self.sites}) != len(self.sites):
            raise ValueError("Los IDs de sites deben ser únicos.")
        if self.bbox:
            west, south, east, north = self.bbox
            if not (-179.9 <= west < east <= 179.9 and -55.9 <= south < north <= 59.9):
                raise ValueError("bbox debe ser [oeste,sur,este,norte] dentro de la cobertura NASADEM, sin cruzar el antimeridiano.")
            if east - west > 1 or north - south > 1:
                raise ValueError("bbox máximo: 1 grado por eje; se muestrean nueve centros, no toda la zona.")
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Para temperatura se requieren start_date y end_date juntos.")
        if self.start_date and not (date(2000, 2, 18) <= self.start_date < self.end_date <= date.today()):
            raise ValueError("Fechas MODIS inválidas; end_date es exclusivo y no futuro.")
        if self.start_date and (self.end_date - self.start_date).days > 32:
            raise ValueError("Periodo térmico máximo: 32 días.")
        if self.start_date and self.window_m < 3000:
            raise ValueError("MODIS requiere window_m de al menos 3000 m para este muestreo.")
        return self


class AnalyzeRequest(StrictModel):
    mission_type: Mission = "mars"
    target_id: Identifier = "jezero"
    data_mode: Literal["demo", "external", "live"] = "demo"
    weights: Weights = Field(default_factory=Weights)
    source: Source | None = None
    earth_engine: EarthEngineQuery | None = None

    @model_validator(mode="after")
    def source_matches_mode(self):
        if (self.data_mode == "live") != (self.source is not None):
            raise ValueError("source es obligatorio y exclusivo de data_mode=live.")
        if (self.source == "google_earth_engine") != (self.earth_engine is not None):
            raise ValueError("earth_engine es obligatorio y exclusivo de source=google_earth_engine.")
        return self


class Provenance(StrictModel):
    source: Text
    product_id: Text
    method: Text
    resolution_m: Annotated[Finite, Field(gt=0, le=1e9)]
    window_m: Annotated[Finite, Field(gt=0, le=1e9)]
    reference_frame: Text

    @model_validator(mode="after")
    def resolution_fits_window(self):
        if self.window_m < self.resolution_m:
            raise ValueError("window_m debe ser al menos resolution_m.")
        return self


class ThermalMeasurement(StrictModel):
    value: Annotated[Finite, Field(gt=0)]
    unit: Literal["K"]
    quantity: Literal["surface_temperature", "brightness_temperature"]
    statistic: Literal["mean"]
    comparison_basis: Text
    quality: Literal["valid"]
    provenance: Provenance


class Region(StrictModel):
    id: Identifier
    name: Text
    body: Literal["earth", "mars", "moon"]
    relief_m: Nonnegative | None = None
    slope_deg: Annotated[Finite, Field(ge=0, le=90)] | None = None
    roughness_m: Nonnegative | None = None
    lat: Annotated[Finite, Field(ge=-90, le=90)] | None = None
    lon: Annotated[Finite, Field(ge=-180, le=180)] | None = None
    provenance: Provenance | None = None
    thermal: ThermalMeasurement | None = None

    @model_validator(mode="after")
    def earth_coordinates(self):
        if self.body == "earth" and (self.lat is None or self.lon is None):
            raise ValueError("Un candidato terrestre necesita lat y lon válidas.")
        return self


class DescriptorDataset(StrictModel):
    schema_version: Literal[2] = 2
    mode: Literal["demo", "external"]
    description: Text
    targets: Annotated[list[Region], Field(min_length=1, max_length=100)]
    candidates: Annotated[list[Region], Field(min_length=3, max_length=100)]

    @model_validator(mode="after")
    def consistent_regions(self):
        rows = self.targets + self.candidates
        if len({row.id for row in rows}) != len(rows):
            raise ValueError("Los identificadores deben ser únicos.")
        if any(row.body == "earth" for row in self.targets) or any(row.body != "earth" for row in self.candidates):
            raise ValueError("targets requiere Luna/Marte; candidates requiere Tierra.")
        if self.mode == "external" and any(row.provenance is None for row in rows):
            raise ValueError("Cada región externa requiere procedencia.")
        return self


class Indicator(StrictModel):
    value: Finite | None
    unit: Literal["normalized_distance", "K"]
    status: Literal["available", "unavailable"]
    message: str


class RankedSite(StrictModel):
    rank: int
    site: Region
    normalized_distance: Nonnegative
    differences: dict[str, Nonnegative]
    topographic: Indicator
    thermal: Indicator
    calibrated_similarity_percent: None = None


class PointGeometry(StrictModel):
    type: Literal["Point"] = "Point"
    coordinates: tuple[Finite, Finite]


class FeatureProperties(StrictModel):
    name: str
    rank: int
    normalized_distance: Nonnegative
    data_mode: Literal["demo", "external", "live"]
    synthetic: bool


class Feature(StrictModel):
    type: Literal["Feature"] = "Feature"
    id: str
    geometry: PointGeometry
    properties: FeatureProperties


class FeatureCollection(StrictModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[Feature]


class AnalyzeResponse(StrictModel):
    mission_type: Mission
    data_mode: Literal["demo", "external", "live"]
    source: Source | None = None
    synthetic: bool
    target: Region
    weights: Weights
    metric: Literal["weighted_normalized_absolute_distance"] = "weighted_normalized_absolute_distance"
    calibrated_similarity_percent: None = None
    top_3: Annotated[list[RankedSite], Field(min_length=3, max_length=3)]
    geojson: FeatureCollection
    messages: list[str]


class ErrorDetail(StrictModel):
    code: str
    message: str


class ErrorResponse(StrictModel):
    detail: ErrorDetail
