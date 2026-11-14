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
`live` requiere `source` y actualmente responde 503 porque la extracción de
productos científicos no está implementada. Nunca cambia silenciosamente a demo.

`source` admite `nasa_earthdata`, `esa_copernicus`, `jaxa_gportal`, `csa_radarsat`
y `nasa_pds_diviner`; no se acepta fuera del modo live. No se reciben URLs,
credenciales, rásteres ni datasets arbitrarios del navegador.

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
| `data_mode`, `synthetic` | Identificación explícita de demo o externo |
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

Los errores controlados comparten `detail.code` y `detail.message`. No se
exponen rutas locales, credenciales ni respuestas crudas de proveedores.

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
const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/api/v1/analyze`, {
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

Configurar `NEXT_PUBLIC_API_URL` con la URL HTTPS de Render; no incluye secretos.
Las credenciales científicas pertenecen exclusivamente al backend/worker.
