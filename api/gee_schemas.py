from typing import Annotated, Literal

from pydantic import Field, model_validator

from api.schemas import Finite, PointGeometry, Region, StrictModel


class TargetCatalog(StrictModel):
    schema_version: Literal[1]
    mode: Literal["external"]
    targets: Annotated[list[Region], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def planetary_provenance(self):
        if any(row.body == "earth" or row.provenance is None for row in self.targets):
            raise ValueError("Los objetivos requieren cuerpo lunar/marciano y procedencia.")
        if len({row.id for row in self.targets}) != len(self.targets):
            raise ValueError("IDs de objetivos duplicados.")
        return self


class LocationProperties(StrictModel):
    region: Region
    elevation_mean_m: Finite | None
    topography_valid_pixels: Annotated[int, Field(ge=0)]
    topography_coverage_fraction: Annotated[Finite, Field(ge=0, le=1)] | None
    thermal_valid_pixels: Annotated[int, Field(ge=0)]
    thermal_image_count: Annotated[int, Field(ge=0, le=8)]
    thermal_first_composite_ms: Annotated[Finite, Field(ge=0)] | None = None
    thermal_last_composite_ms: Annotated[Finite, Field(ge=0)] | None = None
    terrain_reference_period: str = "2000-02-11/2000-02-22 (NASADEM reprocessing v001)"
    messages: list[str]


class LocationFeature(StrictModel):
    type: Literal["Feature"] = "Feature"
    id: str
    geometry: PointGeometry
    properties: LocationProperties


class LocationsResponse(StrictModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    source: Literal["google_earth_engine"] = "google_earth_engine"
    data_mode: Literal["live"] = "live"
    synthetic: Literal[False] = False
    cached: bool = False
    retrieved_at: str
    features: list[LocationFeature]
    messages: list[str]
