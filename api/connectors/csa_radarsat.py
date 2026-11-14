from .base import SourceConnector

connector = SourceConnector(
    "csa_radarsat", "CSA RADARSAT", "EODMS/RCM: cuenta, categoría de acceso y licencia; RADARSAT-2 tiene condiciones comerciales propias.",
    "Productos SAR: procesar calibración y terreno fuera de Render. No son termómetros ni mapas mineralógicos directos.",
)
