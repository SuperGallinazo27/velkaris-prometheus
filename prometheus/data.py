"""Entrada única para ejemplos o futuros descriptores extraídos de datos reales.

No descarga ni interpreta rásteres. La procedencia aportada por quien importa
un archivo no equivale a validación científica.
"""

import json
import math
from pathlib import Path

FEATURES = {
    "relief_m": ("Relieve local (m)", 1000.0),
    "slope_deg": ("Pendiente media (°)", 15.0),
    "roughness_m": ("Rugosidad RMS residual (m)", 100.0),
}
METADATA = (
    "source", "product_id", "method", "resolution_m", "window_m", "reference_frame"
)


def number(value, label, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: se requiere un número.")
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{label}: valor fuera de rango o no finito.")


def validate_dataset(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("El archivo debe ser un objeto JSON con schema_version: 1.")
    if data.get("mode") not in ("demo", "external"):
        raise ValueError("mode debe ser demo o external.")
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        raise ValueError("Falta description.")
    ids = set()
    for group in ("mars", "earth"):
        rows = data.get(group)
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{group}: se necesita una lista no vacía.")
        if group == "earth" and len(rows) != 3:
            raise ValueError("Este MVP requiere exactamente tres candidatos terrestres.")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{group}: cada región debe ser un objeto.")
            for field in ("id", "name"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    raise ValueError(f"{group}: falta {field}.")
            if row["id"] in ids:
                raise ValueError("Los identificadores deben ser únicos.")
            ids.add(row["id"])
            for key in FEATURES:
                number(row.get(key), f"{row['name']} / {key}", maximum=90 if key == "slope_deg" else None)
            if group == "earth":
                number(row.get("lat"), "lat", -90, 90)
                number(row.get("lon"), "lon", -180, 180)
            if data["mode"] == "external":
                for key in METADATA:
                    value = row.get(key)
                    if key in ("resolution_m", "window_m"):
                        number(value, key)
                        if value == 0:
                            raise ValueError(f"{key} debe ser mayor que cero.")
                    elif not isinstance(value, str) or not value.strip():
                        raise ValueError(f"{row['name']}: falta procedencia en {key}.")
                if row["window_m"] < row["resolution_m"]:
                    raise ValueError("window_m debe ser al menos resolution_m.")
    return data


def load_dataset(source):
    """Acepta una ruta o un archivo abierto/subido desde Streamlit."""
    try:
        if hasattr(source, "read"):
            data = json.load(source)
        else:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("No se pudo leer el JSON. Revisa su formato y codificación UTF-8.") from exc
    return validate_dataset(data)
