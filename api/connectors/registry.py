from . import csa_radarsat, esa_copernicus, jaxa_gportal, nasa_earthdata, nasa_pds_diviner

CONNECTORS = {module.connector.source: module.connector for module in (
    nasa_earthdata, esa_copernicus, jaxa_gportal, csa_radarsat, nasa_pds_diviner,
)}
