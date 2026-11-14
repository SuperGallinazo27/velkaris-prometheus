import json

from pydantic import ValidationError

from api.config import ROOT, Settings
from api.errors import AnalysisError
from api.schemas import DescriptorDataset
from prometheus.data import validate_dataset


def load_descriptors(mode: str, settings: Settings) -> DescriptorDataset:
    path = ROOT / "data/demo.json" if mode == "demo" else settings.dataset_path
    if path is None:
        raise AnalysisError(503, "data_not_configured", "No hay descriptores reales configurados. Use data_mode=demo explícitamente.")
    try:
        # Read at most limit+1, even if the file changes after stat(). No raster I/O.
        with path.open("rb") as source:
            raw = source.read(settings.max_dataset_bytes + 1)
        if len(raw) > settings.max_dataset_bytes:
            raise ValueError("El archivo de descriptores supera el límite.")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("mode") != mode:
            raise ValueError("El modo del archivo no coincide con el solicitado.")
        if data.get("schema_version") == 1:
            validate_dataset(data)
            metadata = ("source", "product_id", "method", "resolution_m", "window_m", "reference_frame")

            def convert(row, body):
                converted = {key: value for key, value in row.items() if key not in metadata}
                converted["body"] = body
                if mode == "external":
                    converted["provenance"] = {key: row[key] for key in metadata}
                return converted

            data = dict(schema_version=2, mode=mode, description=data["description"],
                        targets=[convert(row, "mars") for row in data["mars"]],
                        candidates=[convert(row, "earth") for row in data["earth"]])
        return DescriptorDataset.model_validate(data)
    except (OSError, ValueError, TypeError, RecursionError, ValidationError) as exc:
        # No raw source contents, file paths, tokens or provider diagnostics in HTTP.
        raise AnalysisError(503, "invalid_source_data", "Fuente no disponible o inválida: revise formato, tamaño, valores finitos, nodata, unidades y procedencia.") from exc
