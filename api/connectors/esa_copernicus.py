from .base import SourceConnector

connector = SourceConnector(
    "esa_copernicus", "ESA Copernicus", "Cuenta CDSE y OAuth para Sentinel Hub; cuotas y condiciones del producto.",
    "STAC para descubrimiento; Statistical API con dataMask para estadísticas reducidas, sin descargar escenas.",
)
