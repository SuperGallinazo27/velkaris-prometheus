from .base import SourceConnector

connector = SourceConnector(
    "nasa_pds_diviner", "NASA PDS / LRO Diviner", "Archivos PDS generalmente públicos; revisar términos y citas por producto.",
    "Etiquetas PDS, escala/offset, canales, nodata y tiempo solar local; derivados compactos o COG preparado externamente. Diviner corresponde a la Luna.",
)
