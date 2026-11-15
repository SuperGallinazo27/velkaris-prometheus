"""Place catalog and the protected-area rule of the booking portal.

The catalog (data/lugares.json) is written by the frontend's scripts/protegidas/calcular.py from the
World Database on Protected and Conserved Areas (WDPCA, UNEP-WCMC and IUCN). The server decides
with it whether a place can be booked, so the rule never depends on what a browser sends.

The rule is a PROMETHEUS criterion built on the IUCN management categories; it is not a legal
ruling. Only the polygons that contain the point of the place decide. Areas that merely touch the
analysed 18 km box are reported as a warning.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "lugares.json"

# IUCN categories I to IV: the usual reading of "strict protection" in the literature.
STRICT_IUCN = frozenset({"Ia", "Ib", "II", "III", "IV"})
# A proposed area is not in force yet: it is listed but decides nothing.
NOT_IN_FORCE = frozenset({"Proposed"})

# Status of a place for booking. The first four come from the dataset alone.
FREE, PERMIT, PROTECTED, NO_DATA = "libre", "permiso", "protegido", "sin_dato"

IUCN_NAMES = {
    "Ia": "reserva natural estricta",
    "Ib": "área silvestre",
    "II": "parque nacional",
    "III": "monumento natural",
    "IV": "área de manejo de hábitats o especies",
    "V": "paisaje protegido",
    "VI": "área protegida con uso sostenible de los recursos",
}

# Designations of international and regional conventions, as the dataset writes them, in Spanish.
DESIGNATIONS_ES = {
    "World Heritage Site (natural or mixed)": "Sitio del Patrimonio Mundial natural o mixto",
    "UNESCO-MAB Biosphere Reserve": "Reserva de la Biosfera UNESCO-MAB",
    "Wetland of International Importance (Ramsar Site)": "Humedal de Importancia Internacional, sitio Ramsar",
    "UNESCO Global Geopark": "Geoparque Mundial de la UNESCO",
    "Special Protection Area (Birds Directive)": "Zona de Especial Protección para las Aves, Directiva Aves",
    "Special Areas of Conservation (Habitats Directive)": "Zona Especial de Conservación, Directiva Hábitats",
    "Site of Community Importance (Habitats Directive)": "Lugar de Importancia Comunitaria, Directiva Hábitats",
    "Emerald Network": "Red Esmeralda",
    "Marine Protected Area (OSPAR)": "Área Marina Protegida OSPAR",
}
# Countries whose national designations are already written in Spanish in the dataset.
SPANISH_ISO3 = frozenset({"ARG", "BOL", "CHL", "COL", "CRI", "CUB", "DOM", "ECU", "ESP", "GNQ", "GTM", "HND", "MEX",
                          "NIC", "PAN", "PER", "PRI", "PRY", "SLV", "URY", "VEN"})


@dataclass(frozen=True)
class Area:
    site_id: int | None
    name: str
    name_eng: str | None
    designation: str | None
    designation_eng: str | None
    designation_type: str | None
    iucn: str | None
    status: str | None
    status_year: int | None
    governance: str | None
    authority: str | None
    iso3: str | None
    site_type: str | None
    geometry: str

    @property
    def in_force(self) -> bool:
        return self.status not in NOT_IN_FORCE

    @property
    def strict(self) -> bool:
        return self.in_force and self.iucn in STRICT_IUCN

    @property
    def display(self) -> str:
        """The original name, or the English one when the original is not in Latin script."""
        foreign = any(char.isalpha() and ord(char) > 0x24F for char in self.name)
        return self.name_eng if foreign and self.name_eng else self.name

    @property
    def designation_text(self) -> str | None:
        """The designation as people can read it: a convention's name in Spanish, the original
        wording where it is already Spanish, and the dataset's English wording elsewhere."""
        for wording in (self.designation_eng, self.designation):
            if wording in DESIGNATIONS_ES:
                return DESIGNATIONS_ES[wording]
        if self.iso3 in SPANISH_ISO3:
            return self.designation or self.designation_eng
        return self.designation_eng or self.designation

    @property
    def label(self) -> str:
        """«Galápagos (Parque Nacional, categoría UICN II)»."""
        category = f"categoría UICN {self.iucn}" if self.iucn in IUCN_NAMES else "sin categoría UICN informada"
        designation = self.designation_text
        return f"{self.display} ({designation}, {category})" if designation else f"{self.display} ({category})"


@dataclass(frozen=True)
class Place:
    id: str
    name: str
    province: str
    country: str
    latitude: float
    longitude: float
    # None: the place could not be checked against the dataset yet.
    areas: tuple[Area, ...] | None
    nearby: tuple[Area, ...]

    @property
    def in_force(self) -> tuple[Area, ...]:
        return tuple(area for area in self.areas or () if area.in_force)

    @property
    def base_status(self) -> str:
        if self.areas is None:
            return NO_DATA
        if any(area.strict for area in self.areas):
            return PROTECTED
        return PERMIT if self.in_force else FREE

    @property
    def deciding(self) -> tuple[Area, ...]:
        """The areas the status rests on: the strict ones if any, else every area in force."""
        strict = tuple(area for area in self.in_force if area.strict)
        return strict or self.in_force

    @property
    def nearby_strict(self) -> tuple[Area, ...]:
        """Strictly protected areas that touch the analysed box without containing the point."""
        return tuple(area for area in self.nearby if area.strict and area.geometry == "poligono")


@dataclass(frozen=True)
class Catalog:
    source: dict
    places: dict[str, Place]


def _area(raw: dict) -> Area:
    return Area(
        site_id=raw.get("siteId"), name=raw.get("name") or raw.get("nameEng") or "Área sin nombre",
        name_eng=raw.get("nameEng"), designation=raw.get("designation"),
        designation_eng=raw.get("designationEng"), designation_type=raw.get("designationType"),
        iucn=raw.get("iucn"), status=raw.get("status"), status_year=raw.get("statusYear"),
        governance=raw.get("governance"), authority=raw.get("authority"), iso3=raw.get("iso3"),
        site_type=raw.get("siteType"), geometry=raw.get("geometry") or "poligono",
    )


def parse_catalog(data: dict) -> Catalog:
    places = {}
    for raw in data["places"]:
        protected = raw.get("protected")
        places[raw["id"]] = Place(
            id=raw["id"], name=raw["name"], province=raw.get("province") or "", country=raw.get("country") or "",
            latitude=float(raw["latitude"]), longitude=float(raw["longitude"]),
            areas=None if protected is None else tuple(_area(item) for item in protected),
            nearby=tuple(_area(item) for item in raw.get("nearby") or ()),
        )
    return Catalog(source=dict(data.get("source") or {}), places=places)


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    """Read once per process; the file ships with the code and only changes with a deploy."""
    return parse_catalog(json.loads(DEFAULT_PATH.read_text(encoding="utf-8")))


def join(names: list[str]) -> str:
    """«A», «A y B», «A, B y C»."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]


def authority_note(areas: tuple[Area, ...]) -> str:
    known = []
    for area in areas:
        if area.authority and area.authority != "Not Reported" and area.authority not in known:
            known.append(area.authority)
    return f" Autoridad que la base informa: {join(known[:2])}." if known else ""


def dataset_reason(place: Place) -> str:
    """Why the dataset alone gives the place its status, in plain Spanish."""
    status = place.base_status
    if status == NO_DATA:
        return "Este lugar aún no se pudo consultar en la base de áreas protegidas, así que no se puede reservar."
    if status == FREE:
        return "La base de áreas protegidas no registra ninguna en este punto."
    if status == PROTECTED:
        return (f"{inside(place)}: protección estricta. PROMETHEUS no asigna turnos en áreas de "
                f"protección estricta (categorías UICN Ia a IV).{authority_note(place.deciding)}")
    return (f"{inside(place)}. Solo se puede reservar declarando el permiso de la autoridad del "
            f"área.{authority_note(place.deciding)}")


def inside(place: Place) -> str:
    """«Está dentro de Galápagos (Parque Nacional, categoría UICN II)», with up to three areas."""
    return "Está dentro de " + join([area.label for area in place.deciding[:3]])
