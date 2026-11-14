# Google Earth Engine: localización y evaluación de candidatos terrestres

Integración con la [biblioteca Python oficial](https://developers.google.com/earth-engine/guides/python_install),
`earthengine-api==1.7.52`. La API construye cálculos en GEE y recibe estadísticas
reducidas. No descarga ni exporta GeoTIFF, NetCDF o imágenes completas a Render.
La activación de credenciales y la primera comprobación real quedan pendientes
de la configuración del propietario en Render.

## Qué hace y qué no demuestra

- Recibe 1–9 coordenadas terrestres o una caja geográfica pequeña; una caja
  genera nueve centros de muestreo regulares. Devuelve un GeoJSON localizable.
- Calcula relieve local, pendiente media y RMS residual respecto a un plano.
  Puede obtener temperatura superficial diurna con QA para fechas explícitas.
- Compara tres o más candidatos con un objetivo planetario externo mediante
  el motor existente y devuelve Top 3. No usa objetivos demo con Tierra real.
- No busca exhaustivamente todo el planeta ni identifica automáticamente
  mineralogía, riesgos operativos o lugares seguros para robots. No hay un
  porcentaje calibrado ni una certificación de equivalencia planetaria.

Esto permite **preselección exploratoria** de lugares para campañas análogas.
Un DEM de 30 m no resuelve obstáculos a escala de ruedas, adherencia, resistencia
del suelo, pendientes locales pequeñas o permisos de acceso. Esas decisiones
requieren levantamientos de campo y un modelo del robot por separado.

## Productos y métodos

| Variable | Producto / resolución | Procedimiento |
|---|---|---|
| Elevación / relieve | NASA/NASADEM_HGT/001, aproximadamente 30 m | Enmascarar agua (`swb != 0`) y códigos `num` 0/255; reproyectar bilineal a UTM local de 30 m; max–min en ventana cuadrada |
| Pendiente | mismo DEM reproyectado | `ee.Terrain.slope`, grados, media espacial |
| Rugosidad | mismo DEM | Mínimos cuadrados `z = a + bx + cy`; RMS de residuos devuelto por `ee.Reducer.linearRegression(3,1)`; no sustituir por desviación estándar de elevación |
| Temperatura superficial diurna | MODIS/061/MOD11A2, 1 km, compuestos de 8 días | `QC_Day == 0`, DN válido 7500–65535, multiplicar por 0.02 K; media de compuestos disponibles por píxel y luego media espacial |

La elevación NASADEM usa EGM96, con observaciones originales de febrero de 2000.
La elevación media se reporta como contexto y no entra en la distancia entre
planetas. [Catálogo NASADEM](https://developers.google.com/earth-engine/datasets/catalog/NASA_NASADEM_HGT_001).

La temperatura no es aire ni clima en tiempo real. La selección se basa en las
fechas de inicio de compuestos: no representa una media diaria ponderada exacta
del intervalo solicitado. MOD11A2 ya promedia observaciones diarias y su QA
describe el compuesto; filtrar QA no revierte esa agregación.
[Catálogo MOD11A2 y bits QA](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD11A2).

Método topográfico versionado: `prometheus_topography_plane_v1`. Ventana cuadrada
en proyección métrica local, resolución efectiva de 30 m, mismo procedimiento
de reproyección, pendiente y plano residual. Los objetivos planetarios deben
prepararse con un sistema métrico adecuado a su cuerpo y ese protocolo; no usar
UTM terrestre para coordenadas lunares/marcianas. La API exige coincidencia del
identificador de método, resolución y tamaño de ventana, pero no puede certificar
la autenticidad de los metadatos. **Recalcular el objetivo; no renombrar el método
de unos descriptores incompatibles para pasar la validación.**
[Regresión y RMS oficial](https://developers.google.com/earth-engine/apidocs/ee-reducer-linearregression).

## Activación posterior en Render

1. Registrar el proyecto Google Cloud para Earth Engine y habilitar la API.
   Configurar permisos de la cuenta de servicio para el proyecto y productos.
   Seguir [autenticación](https://developers.google.com/earth-engine/guides/auth)
   y [cuentas de servicio oficiales](https://developers.google.com/earth-engine/guides/service_account).
2. Añadir el JSON de la cuenta de servicio como **Secret File de Render**, por
   ejemplo `earth-engine-service-account.json`. No subirlo a GitHub ni enviarlo
   al frontend o al chat. El SDK usa ese archivo solo dentro del worker.
3. Configurar:

```text
GEE_ENABLED=true
GEE_PROJECT_ID=<ID del proyecto registrado>
GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/earth-engine-service-account.json
```

El Blueprint incluye `GEE_ENABLED=false` inicialmente. Al activar de forma
permanente un servicio gestionado por Blueprint, actualizar también ese valor
en su configuración declarativa para que futuras sincronizaciones no lo reviertan.
No se necesita ninguna clave OpenAI. Configuración del proyecto, uso comercial
o no comercial y cuotas son propios de Google Earth Engine.

4. Probar `/api/v1/earth-engine/locations` con una coordenada y sin temperatura.
   Después probar un periodo histórico conocido. Un estado
   `configured_unverified` en `/api/v1/sources` solo confirma variables, **no**
   permisos ni éxito remoto. Los errores se devuelven como 503 sin secretos.
5. Para ranking planetario, añadir además `PROMETHEUS_TARGETS_PATH` apuntando a
   un JSON reducido preparado con mediciones reales y procedencia.

El cálculo sigue en un subprocess separado: inicialización Google, construcción
del grafo y una evaluación `getInfo()` reducida. Se evita bloquear el event loop
web y se mata el proceso al agotar tiempo o cancelar la petición. Eso limita
la espera local; no garantiza cancelar trabajo ya aceptado por Google.

## Localizar y muestrear

`POST /api/v1/earth-engine/locations`:

```json
{
  "sites": [{"id": "zona-a", "name": "Zona a estudiar", "lat": -24.5, "lon": -69.2}],
  "window_m": 3000,
  "start_date": "2024-01-01",
  "end_date": "2024-01-17"
}
```

Son coordenadas de consulta, no afirmaciones de aptitud ni mediciones de ejemplo.
Omitir ambas fechas evita el cálculo térmico. `end_date` es exclusivo.
Para explorar una zona sustituir `sites` por:

```json
{"bbox": [-69.3, -24.6, -69.1, -24.4], "window_m": 3000}
```

La caja usa [oeste, sur, este, norte]; se muestrean nueve centros. Las ventanas
pueden cruzar sus límites o solaparse: el resultado no cubre toda la caja.

Respuesta: `FeatureCollection`, `source=google_earth_engine`, `data_mode=live`,
`synthetic=false`, `retrieved_at`, `cached` y `features`. Cada punto usa [lon,lat].
En `feature.properties.region` están los descriptores y la procedencia; también
se reportan recuentos, cobertura, periodo del terreno y fechas de compuestos.
`null` significa ausente, nunca cero. Agua/sin cobertura, pocas muestras o QA
insuficiente no se reemplazan por datos ficticios.

## Ranking análogo

`POST /api/v1/analyze`:

```json
{
  "mission_type": "mars",
  "target_id": "id-de-tu-objetivo-verificado",
  "data_mode": "live",
  "source": "google_earth_engine",
  "earth_engine": {"bbox": [-69.3, -24.6, -69.1, -24.4], "window_m": 3000},
  "weights": {"relief_m": 1, "slope_deg": 1, "roughness_m": 1}
}
```

`PROMETHEUS_TARGETS_PATH`: objeto con `schema_version: 1`, `mode: external` y
`targets`: lista de `Region` (esquema OpenAPI), cada una de cuerpo `mars|moon`,
con ID único y procedencia. No necesita candidatos terrestres almacenados.
Alternativamente se admite el dataset externo completo ya soportado por la API.
No se incluye un archivo con valores planetarios inventados.

La respuesta conserva Top 3/GeoJSON y añade `source=google_earth_engine` y
`data_mode=live`. Una variable activa ausente en cualquier sitio produce 422;
no se eliminan candidatos silenciosamente. Los datos térmicos solo se comparan
si coincide exactamente la base de comparación: normalmente un dato planetario
no será directamente comparable con MODIS y el indicador será `null`.

## Límites efectivos y errores

- 1–9 sitios; 3–9 para ranking, o 9 centros generados por bbox.
- Cobertura de entrada conservadora NASADEM: latitudes −55.9 a 59.9,
  longitudes −179.9 a 179.9. Caja de hasta 1° por eje, sin antimeridiano.
- Ventana 1000–5000 m; temperatura requiere al menos 3000 m. 40000 píxeles
  por reducción topográfica; escala 30 m, `bestEffort=false` (sin degradación
  silenciosa). Para térmico, máximo 100 píxeles por ventana a 1 km.
- Periodo térmico máximo 32 días, máximo 8 compuestos; colección fija, sin URLs
  o expresiones arbitrarias. Cobertura topográfica mínima 80% y cuatro píxeles:
  umbral operativo de completitud, no certificación científica.
- Una consulta GEE simultánea por proceso; seis consultas nuevas/minuto.
  Caché de hasta 32 consultas/15 minutos, conservando `retrieved_at` original.
- Deadline total del worker 25 s, timeout por llamada SDK 10 s, sin reintentos
  SDK; respuesta reducida máxima 64 KiB. Petición HTTP total: 35 s.
- 422: entrada/método/variables incompatibles; 503: desactivado, acceso fallido,
  fuente inválida, ocupado o timeout; 429: presupuesto de consultas excedido.

Los límites y caché son por proceso y se reinician al desplegar. Para varios
workers o acceso público amplio, compartir cuotas y autenticación en un gateway.
CORS no es autenticación. Configurar cuotas/presupuesto de Google antes de
habilitar consultas de terceros. La demo no consume Earth Engine.

## Frontend separado

Repositorio inspeccionado: [frontend-velkaris-prometheus](https://github.com/SuperGallinazo27/frontend-velkaris-prometheus).
Es **Vite**, usa `VITE_API_BASE_URL` y actualmente importa datos demo locales;
sus rutas `/regions`, `/candidates`, `/rankings` eran propuestas, no integraciones.
Esta entrega modifica el backend; el frontend no empieza a consultar GEE por
el solo hecho de desplegarlo. Un cliente puede usar:

```typescript
const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/api/v1/earth-engine/locations`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ bbox: [-69.3, -24.6, -69.1, -24.4], window_m: 3000 }),
  signal: abortController.signal
});
const data = await response.json();
if (!response.ok) throw new Error(data.detail?.message ?? "Consulta no disponible");
// Validar FeatureCollection y campos nulos antes de mostrar datos en el mapa.
```

Su tipo actual `Terrain` requiere números: `relief` ← `region.relief_m`, `slope`
← `slope_deg`, `roughness` ← `roughness_m`, `latitude` ← `lat`, `longitude` ← `lon`.
Debe admitir nulos y procedencia antes de consumir datos reales; nunca convertir
ausencia a cero ni ejecutar su ranking local sobre mediciones incompletas.
Para la comparación, consumir el ranking `/analyze` del backend como autoridad.
Configurar el origen HTTPS exacto del frontend en `CORS_ORIGINS` de Render;
el nombre del repositorio no determina su dominio Vercel.
