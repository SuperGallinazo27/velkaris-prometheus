# Datos geoespaciales sin rásteres pesados en Render

## Estado real de la implementación

Funciona hoy: demo existente, archivos reducidos v1/v2 con procedencia, cálculo
original y pesos opcionales, Top 3/GeoJSON, errores explícitos, límites HTTP y
transporte reutilizable de JSON + una página STAC, probado con mocks.

Los cinco módulos por fuente exponen capacidades y fallan con 503 en `live`.
Son **contratos de integración**, no clientes científicos completos. Añadir una
clave no los activa. Un sexto conector, **Google Earth Engine**, implementa reducción
remota NASADEM/MODIS por coordenadas: [configuración y contrato](EARTH_ENGINE.md).
La lectura COG sigue siendo una estrategia pendiente de implementación.
No se anuncia tiempo real: los archivos
planetarios y satelitales tienen fechas de adquisición, latencias y revisiones.

## Flujo propuesto por producto

1. Seleccionar catálogo, colección, variable/banda y versión autorizados desde
   configuración del operador. Nunca aceptar una URL de ráster del navegador.
2. Consultar metadatos con AOI, intervalo de fechas y un máximo de resultados.
   STAC describe/descubre activos: por sí solo no calcula elevación, temperatura
   ni mineralogía. [PySTAC Client](https://pystac-client.readthedocs.io/en/stable/usage.html)
   permite descubrimiento cuando el catálogo soporta las clases de conformidad.
3. Preferir reducción en origen: Sentinel Hub Statistical, GEE reduceRegion o
   un servicio específico del producto. Traer solo estadísticas + QA/procedencia.
4. Si existe COG genuino con HTTP Range, leer una ventana pequeña y su overview
   apropiado. Nunca `read()` de toda la imagen ni `requests.get(...).content`
   sobre GeoTIFF/NetCDF/IMG. Si no se puede asegurar acceso parcial, responder
   «fuente no soportada» o preparar derivados en un worker externo.
5. Convertir a descriptores consistentes, enmascarando nodata y píxeles inválidos,
   aplicando scale/offset y unidades. Guardar colección/producto, versión, fechas,
   método, resolución efectiva, ventana, CRS/datum y QA. Validar el JSON reducido
   antes de entregarlo al motor existente.

## Fuentes: no existe un endpoint homogéneo

| Fuente / módulo | Acceso y reducción | Cuenta/licencia y límites científicos |
|---|---|---|
| NASA EarthData / `nasa_earthdata.py` | CMR/STAC según colección; Harmony para subconjuntos soportados; COG si el activo lo permite | Earthdata Login para transformaciones Harmony y otros servicios protegidos. La disponibilidad de DEM/LST y subset depende del producto. [Harmony](https://harmony.earthdata.nasa.gov/docs) |
| ESA Copernicus / `esa_copernicus.py` | CDSE/STAC para descubrimiento; Sentinel Hub Statistical para estadísticas en origen y `dataMask` | Cuenta y OAuth para Sentinel Hub; cuotas/condiciones del servicio. Sentinel-2 no aporta temperatura superficial directa. [Statistical API](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Statistical.html), [autenticación](https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/UserGuides/BeginnersGuide.html) |
| JAXA G-Portal / `jaxa_gportal.py` | Catálogo/formatos por misión; no suponer una API genérica de estadísticas ni COG en todos los productos | Registro para accesos que lo requieren y condiciones por producto. Si solo entrega archivos completos, procesarlos fuera de Render. [Registro oficial](https://www.gportal.jaxa.jp/user/regist?lang=en&mode=regist) |
| CSA RADARSAT / `csa_radarsat.py` | EODMS/RCM y productos SAR; preprocesar calibración y derivados en otro entorno | Elegibilidad/categoría de acceso y licencia por producto. [RCM FAQ](https://www.asc-csa.gc.ca/eng/satellites/radarsat/faq.asp), [licencia](https://www.asc-csa.gc.ca/eng/satellites/radarsat/access-to-data/public-user-license-agreement.asp). RADARSAT-2 tiene acceso comercial propio: [pedidos CSA](https://www.asc-csa.gc.ca/eng/satellites/radarsat2/order-contact.asp) |
| NASA PDS/LRO Diviner / `nasa_pds_diviner.py` | PDS/ODE y productos derivados, etiquetas PDS3/PDS4; convertir fuera de Render cuando no sean parciales | Archivos públicos en general; revisar referencias y términos del producto. Diviner es lunar, no una fuente térmica de Marte. [Archivo oficial](https://pds-geosciences.wustl.edu/missions/lro/diviner.htm) |

GEE es una plataforma de procesamiento, no una agencia ni un catálogo universal
de datos planetarios. Requiere proyecto registrado y autenticación apropiada,
por ejemplo una cuenta de servicio para ejecución no interactiva.
[Autenticación GEE](https://developers.google.com/earth-engine/guides/auth).

## Ventanas COG y estadísticas reducidas

En un worker autorizado con rasterio, transformar la AOI al CRS del activo,
calcular una `Window`, intersectarla con sus límites y comprobar tamaño y bloques
**antes** de leer. Usar `src.read(band, window=window, out_shape=(h,w), masked=True)`
con una resolución explícita. La ventana no garantiza bytes mínimos: Rasterio
lee bloques completos; un ráster sin bloques adecuados puede obligar a leer
demasiado. Tampoco `out_shape` limita por sí solo la transferencia.
[Lecturas por ventana](https://rasterio.readthedocs.io/en/latest/topics/windowed-rw.html).

Por eso el futuro worker debe comprobar COG/tiling, soporte HTTP Range (206),
tamaño de bloques/overviews y bytes transferidos, sin fallback a descargar todo.
Limitar memoria/caché GDAL, conexiones, timeout y reintentos. Para límites duros
de tiempo/bytes usar proceso cancelable y transporte medido; cancelar una tarea
async no detiene una lectura GDAL bloqueante en un hilo.

Para GEE usar selección de bandas + máscara QA + AOI acotada y `reduceRegion`
con `scale`, CRS y `maxPixels` explícitos; recuperar solo el diccionario. Evitar
exportar imágenes al disco. `bestEffort` cambia resolución: deshabilitarlo si
rompe comparabilidad, o registrar esa resolución efectiva. No llamar `getInfo()`
síncrono dentro del event loop web. [Reducción regional GEE](https://developers.google.com/earth-engine/guides/reducers_reduce_region).

PDS no implica COG ni STAC. IMG, tablas, JP2 y etiquetas necesitan interpretación
por producto; un servicio de catálogo no es un servicio de estadísticas.
Diviner puede incluir -9999 para temperatura ausente: enmascarar según etiquetas
y QA, nunca promediar ese sentinel. Conservar canal, hora solar, estación y
tipo de temperatura. Temperatura de brillo y temperatura superficial no son
intercambiables; esta API las distingue.
[Nota de datos Diviner](https://pds-geosciences.wustl.edu/missions/lro/diviner.htm).

## Límites

| Límite | Implementado en esta entrega |
|---|---|
| Cuerpo de análisis | 16 KiB contados durante recepción; 413 al exceder |
| Archivo reducido | lectura de hasta 1 MiB + 1 byte; 503 al exceder |
| Regiones | 1–100 objetivos, 3–100 candidatos; siempre Top 3 |
| Petición | 35 s incluyendo espera, cuerpo y análisis; 503 al agotar |
| Cálculos simultáneos | 4 por proceso; Uvicorn limita 32 conexiones/tareas |
| Transporte JSON auxiliar | 1 MiB, deadline total de 8 s, HTTPS/hosts aprobados, sin redirects |
| STAC auxiliar | una página, máximo 20 items; no sigue `next` ni activos |
| Rásteres | ninguno se abre ni descarga en la API actual |
| Worker GEE | 25 s, 64 KiB de salida, una consulta simultánea; seis nuevas por minuto/proceso |

Para futuros workers COG, adoptar inicialmente 512×512 píxeles efectivos por banda
(262144), pocas bandas y una AOI/intervalo temporal máximos por colección;
rechazar consultas mayores. `QueryBudget.max_pixels` reserva ese presupuesto,
pero **no es un límite de ráster activo**, porque no existe lector de ráster en
la API. Ningún límite de tiempo garantiza por sí solo un techo de RAM.

Cachear resultados reducidos fuera del disco efímero con clave de producto,
versión, AOI, periodo, resolución, método y máscara QA; nunca mezclar fuentes o
misiones. Autenticación, rate limiting y caché compartida son trabajo previo a
activar consultas públicas con cuotas. Preparar los productos pesados en batch
reduce coste y permite revisión científica antes de servirlos.

## Comparabilidad

Relieve local no equivale a elevación absoluta entre datums planetarios.
Pendiente/rugosidad dependen de resolución, proyección, tamaño de ventana y método.
Un DEM no mide temperatura; SAR no mide mineralogía directamente. Estimar
mineralogía requiere productos/espectros y métodos específicos validados.
El contrato actual no fabrica esas variables ni las usa para puntuar.
