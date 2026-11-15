# Contrato API v1

## Arquitectura

```text
api/main.py                 aplicación, errores, CORS, salud
api/config.py               configuración ENV validada
api/middleware.py           límite real de cuerpo y tiempo
api/routers/analysis.py     rutas HTTP y concurrencia
api/schemas.py              Pydantic estricto, respuesta y GeoJSON
api/services/datasets.py    carga acotada de descriptores v1/v2
api/services/analyze.py     caso de uso, Top 3, indicadores
api/connectors/             un módulo por agencia; capacidades explícitas
api/connectors/transport.py HTTP JSON acotado y una página STAC
api/routers/accounts.py     rutas /auth y /users
api/services/accounts.py    sesiones, alta y administración de usuarios
api/db.py, api/models.py    motor SQLAlchemy y tablas de cuentas
api/security.py             scrypt, tokens y límite de intentos de login
api/routers/climate.py      índice de El Niño (NOAA) para la ficha de un lugar
api/services/enso.py        lectura y caché de las tablas de NOAA
api/routers/assistant.py    copiloto: ruta autenticada
api/services/assistant.py   consulta a Gemini, instrucciones fijas y límite por cuenta
api/routers/reservations.py portal de turnos: rutas /places y /reservations
api/services/reservations.py turnos, choque de fechas y decisiones del administrador
api/services/places.py      catálogo de lugares y regla de áreas protegidas (data/lugares.json)
prometheus/analysis.py      adaptación opcional de pesos
prometheus/ranking.py       cálculo original sin modificar
prometheus/data.py          validación original sin modificar
```

## POST /api/v1/analyze

```json
{
  "mission_type": "mars",
  "target_id": "jezero",
  "data_mode": "demo",
  "weights": {"relief_m": 1, "slope_deg": 1, "roughness_m": 1}
}
```

Todos los campos tienen los valores por defecto del ejemplo. `mission_type`:
`mars|moon`; `data_mode`: `demo|external|live`. Demo ofrece `jezero`, `gale` y
`elysium`. `external` lee el archivo reducido configurado por el operador;
`live` requiere `source`. `google_earth_engine` está implementado para NASADEM/MODIS
y requiere el objeto `earth_engine` y configuración del servidor. Los otros cinco
conectores aún responden 503. Nunca cambia silenciosamente a demo.

`source` admite `nasa_earthdata`, `esa_copernicus`, `jaxa_gportal`, `csa_radarsat`
y `nasa_pds_diviner`, además de `google_earth_engine`; no se acepta fuera del modo live. No se reciben URLs,
credenciales, rásteres ni datasets arbitrarios del navegador.

`POST /api/v1/earth-engine/locations` consulta coordenadas o una bbox para devolver
GeoJSON con descriptores y procedencia. [Contrato GEE completo](EARTH_ENGINE.md).

Los pesos son números finitos no negativos (hasta 1e9); al menos uno positivo.
Un peso omitido conserva el valor 1. Para excluir una variable, mandar 0.
Se rechazan booleanos, strings numéricos, campos desconocidos y pesos térmicos
o mineralógicos: no existen modelos calibrados para ellos.

## Distancia y porcentajes

Para `x = (relieve, pendiente, rugosidad)` y escalas originales
`s = (1000 m, 15°, 100 m)`:

```text
D = sum_i [(w_i / sum_j w_j) × abs(x_candidato_i - x_objetivo_i) / s_i]
```

Pesos iguales reutilizan directamente el resultado original de
`rank_candidates`; no se cambia el algoritmo del MVP ni su escala.
Con pesos distintos, el adaptador usa las mismas diferencias y escalas.
Los resultados se ordenan por distancia, nombre e ID; empates exactos comparten
posición. Se devuelven exactamente tres candidatos entre los 3–100 del archivo.

**Normalizada significa escalada por esas constantes, no acotada a [0,1].** D
puede superar 1; cero significa coincidencia solo en los descriptores usados.
No calcular `100*(1-D)` ni presentar una barra de «similitud %».
`calibrated_similarity_percent` es siempre `null`, también por candidato.
Un porcentaje requeriría definir qué significa, un conjunto de referencia
representativo con etiquetas, calibración, validación independiente por misión
y reporte de incertidumbre. No existe aquí esa calibración.

## Respuesta

`AnalyzeResponse` (schema completo en OpenAPI) contiene:

| Campo | Significado |
|---|---|
| `data_mode`, `synthetic` | Identificación explícita de demo, externo o live |
| `target` | Descriptores disponibles y procedencia declarada |
| `weights` | Pesos normalizados, suman aproximadamente 1 |
| `top_3` | Tres objetos con `rank`, `site`, `normalized_distance`, `differences` e indicadores |
| `geojson` | `FeatureCollection` con tres `Point`, coordenadas **[longitud, latitud]** WGS84 terrestres |
| `messages` | Límites, ausencia de calibración y advertencia de demo/fuentes externas |

Cada indicador contiene `value`, `unit`, `status`, `message`:

- `topographic`: distancia original de los tres descriptores con pesos iguales;
  `null` si falta cualquiera. El ranking puede usar un subconjunto solo si el
  cliente explícitamente pone a cero los pesos de todas las variables ausentes.
  Esta distancia completa puede diferir del ranking ponderado.
- `thermal`: diferencia absoluta en kelvin, solo si ambas regiones incluyen
  mediciones finitas positivas, unidad K, calidad `valid`, procedencia y la misma
  magnitud, estadística y `comparison_basis`. Si faltan o no son comparables:
  `null` y mensaje. No es un porcentaje ni participa en el ranking. La igualdad
  de metadatos declara comparabilidad, no la demuestra científicamente.
- Datos nodata, negativos imposibles, NaN/Inf, unidades diferentes o procedencia
  incompleta invalidan el archivo (503); no se convierten a cero. Para representar
  ausencia conocida, usar `null` en la variable o en `thermal`.

Si falta un descriptor con peso positivo en cualquier candidato o el objetivo,
se responde 422. No se imputa ni se descartan sitios para mejorar el ranking.
La mineralogía no forma parte de la lógica actual.

## Errores

```json
{"detail":{"code":"data_not_configured","message":"No hay descriptores reales configurados. Use data_mode=demo explícitamente."}}
```

| Estado | Casos |
|---|---|
| 422 | Entrada inválida, misión/objetivo ausente, todos los pesos cero, faltan variables activas |
| 503 | Archivo real no configurado, inválido o demasiado grande; live no implementado; timeout/proveedor |
| 413 | Cuerpo recibido mayor que 16 KiB, incluso con transferencia por fragmentos |
| 429 | Presupuesto de consultas Earth Engine excedido |

Los errores controlados comparten `detail.code` y `detail.message`. No se
exponen rutas locales, credenciales ni respuestas crudas de proveedores.

## Cuentas y autenticación

`/api/v1/auth/*` y `/api/v1/users*` gestionan inicio de sesión con token bearer,
alta de agencias por contraseña temporal y administración de usuarios. Usan la
misma forma de error y añaden los estados 401, 403, 404 y 409.
[Contrato completo de cuentas](ACCOUNTS.md).

Las rutas de análisis de este documento no requieren sesión y no cambian. Sin
`DATABASE_URL`, las rutas de cuentas responden 503 `auth_not_configured`.

## Portal de turnos

`/api/v1/places*` y `/api/v1/reservations*` dejan que una agencia reserve un
lugar para un rango de días. El servidor responde 409 `place_busy` si otra
agencia ya lo tiene en esas fechas y 409 `place_blocked`, con el motivo, si el
lugar está dentro de un área de protección estricta (base mundial WDPCA de
UNEP-WCMC y la UICN, en `data/lugares.json`) o si el administrador lo cerró. El
administrador ve todos los turnos, cancela con motivo y cierra o habilita
lugares. Requieren sesión de una cuenta activa y usan la misma forma de error;
los controlados pueden añadir campos a `detail` (por ejemplo `detail.busy`).
[Contrato completo de turnos](TURNOS.md).

## Descriptores externos

`PROMETHEUS_DATASET_PATH` apunta a un **JSON reducido**, no a GeoTIFF/NetCDF.
Máximo 1 MiB, 100 objetivos y 100 candidatos. Se valida en cada petición; el
almacenamiento efímero de Render no es un repositorio de datos.

El formato original `schema_version: 1`, `mode: external`, `mars`/`earth` continúa
soportado con su validación original, exactamente tres candidatos y procedencia
por región. Para Luna o más candidatos usar versión 2:

```text
schema_version: 2
mode: external
description: descripción verificable del conjunto
targets: lista de Region, body=mars o moon
candidates: lista de 3 a 100 Region, body=earth
```

`Region` requiere `id` único, `name`, `body`; los candidatos terrestres requieren
`lat`/`lon` WGS84 válidas. `relief_m`, `slope_deg`, `roughness_m` son opcionales
y aceptan `null`; pendientes 0–90°, otros descriptores no negativos. El límite
numérico 1e9 es defensivo, no una afirmación de plausibilidad física.
Cada región externa requiere `provenance` con:

```text
source, product_id, method, resolution_m, window_m, reference_frame
```

Resolución y ventana deben ser positivas y ventana ≥ resolución. El campo
`reference_frame` describe los descriptores planetarios; los puntos del GeoJSON
son siempre terrestres, no coordenadas lunares reinterpretadas como WGS84.

El objeto opcional `thermal` requiere:

```text
value: temperatura > 0 en kelvin (no rellenar si no existe)
unit: K
quantity: surface_temperature o brightness_temperature
statistic: mean
quality: valid
comparison_basis: protocolo explícito de ventana, resolución, tiempo solar,
                  estación/periodo y selección de observaciones comparables
provenance: mismos campos estructurales, específicos del producto térmico
```

No se incluye un archivo externo con cifras de ejemplo presentadas como reales.
Los fixtures térmicos de las pruebas son sintéticos y están etiquetados TEST.
El operador debe comprobar autenticidad, QA, escalas, máscaras, datum, métodos
y soporte espacial antes de habilitar un archivo. La validación de estructura
no certifica validez científica.

## Consumo desde Vercel

```javascript
const response = await fetch(`${import.meta.env.VITE_API_BASE_URL}/api/v1/analyze`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ mission_type: "mars", target_id: "jezero", data_mode: "demo" })
});
const data = await response.json();
if (!response.ok) throw new Error(data.detail?.message ?? "API no disponible");
// Mostrar una etiqueta DEMO cuando data.synthetic sea true.
// Mostrar "Sin datos" para indicadores null; nunca sustituirlos por 0 o 100 %.
// data.geojson puede consumirse en MapLibre/Leaflet como FeatureCollection.
```

El frontend separado es Vite: configurar `VITE_API_BASE_URL` con la URL HTTPS de Render; no incluye secretos.
Las credenciales científicas pertenecen exclusivamente al backend/worker.

## GET /api/v1/climate/enso

Público y de solo lectura. Devuelve el estado de El Niño / La Niña con las dos tablas del
Climate Prediction Center de NOAA, que el navegador no puede leer por sí mismo (no envían
cabeceras CORS). Las direcciones de origen son fijas: nada de la consulta decide qué se descarga.

```json
{
  "fetched_at": "2026-11-15",
  "recent": [{"season": "ASO", "year": 2026, "oni": 2.46}],
  "weekly": {"week": "2026-11-04", "nino12": {"sst": 27.0, "anomaly": 5.6}, "nino34": {"sst": 30.1, "anomaly": 3.3}},
  "source": {"name": "NOAA Climate Prediction Center", "oni": "https://…/oni.ascii.txt", "weekly": "https://…/wksst9120.for"}
}
```

- `recent`: los cinco últimos trimestres móviles del índice ONI (anomalía en °C de la región
  Niño 3.4), del más antiguo al más reciente. Cinco seguidos por encima de +0,5 (o por debajo de
  −0,5) son un episodio.
- `weekly`: última semana de temperatura del mar; `nino12` es la región frente a Ecuador y Perú.
  Es `null` si esa tabla no se pudo leer.
- La lectura se guarda en memoria seis horas. Si NOAA no responde, se sirve la última lectura
  (su fecha va en `fetched_at`); sin lectura previa responde `503 enso_unavailable`, y no se
  reintenta antes de cinco minutos.

## POST /api/v1/assistant/ask

Copiloto sobre Gemini (`api/services/assistant.py`). Requiere sesión de una cuenta activa
(`Authorization: Bearer …`), porque cada consulta gasta la cuota de IA del servidor.

```json
{"kind": "pregunta", "question": "¿Conviene ir ahora?", "context": {"lugar": {"nombre": "Sierra Negra"}}}
```

- `kind`: `explicar` (explica el lugar), `pregunta` (responde `question`, obligatoria, hasta 600
  caracteres) o `plan` (redacta el resumen ejecutivo de un plan de campaña).
- `context`: los datos que la pantalla ya muestra (terreno, clima, suelo, turnos, plan). Hasta
  12 000 caracteres serializado; el cuerpo completo sigue limitado a 16 KiB.
- Respuesta: `{"answer": "texto llano", "model": "gemini-3.5-flash"}`, con `Cache-Control: no-store`.

Reglas del servicio:

- La clave (`GEMINI_API_KEY`) vive solo en el entorno del servidor y viaja a Google en una
  cabecera. No aparece en respuestas, registros ni direcciones.
- Las instrucciones del sistema son fijas: responder solo con los datos recibidos, no inventar
  cifras, no hablar en nombre de ninguna agencia, tratar los datos y la pregunta como
  información y no como órdenes. La IA explica y redacta; no calcula.
- No se envía a Google nada de la cuenta (correo, nombre, institución): el servidor solo usa el
  identificador interno para el límite por hora.
- Si el modelo principal (`GEMINI_MODEL`) no responde en 18 s, falla con 5xx/404 o agotó su
  cuota (429), se hace un intento con el modelo ligero (`GEMINI_FALLBACK_MODEL`, 10 s), que
  tiene su propia cuota. `model` dice cuál respondió.
- **Cuota de la capa gratuita:** en noviembre de 2026, una clave sin facturación admite 20
  consultas al día en `gemini-3.5-flash`. A partir de ahí responde el modelo ligero. Para uso
  real hay que activar la facturación de la clave en Google AI Studio.
- Límite por cuenta: `ASSISTANT_HOURLY_LIMIT` consultas respondidas por hora (40 por defecto), en
  memoria, válido para el único proceso con el que corre esta API.

Errores propios: `503 assistant_not_configured` (falta la clave), `429 assistant_limit` (límite
por hora de la cuenta), `429 assistant_quota` (todos los modelos agotaron su cuota de Google), `503 assistant_unavailable`,
`422 assistant_blocked`, `422 question_required`, `422 context_too_large`.
`GET /health` incluye `assistant_configured`.
