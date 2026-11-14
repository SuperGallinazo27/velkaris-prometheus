# Backend en Render; frontend en Vercel

## Backend

Este repositorio contiene el backend Python. Crear en Render un **Blueprint**
conectado a `SuperGallinazo27/velkaris-prometheus`, rama **main**, archivo
`render.yaml`. El servicio `prometheus-api` queda ligado explícitamente a `main`
y despliega automáticamente sus nuevos commits (`autoDeployTrigger: commit`).
El archivo configura el servicio cuando Render lo importa; guardarlo en GitHub
no crea por sí solo un servicio ni migra un proyecto existente de Vercel.

Si el Web Service ya existe y se creó manualmente, configurar los valores de la
tabla en su panel o vincularlo mediante un Blueprint. Confirmar que su rama sea
`main`. No crear un segundo servicio si ya hay uno para este backend.

| Configuración | Valor |
|---|---|
| Runtime | Python |
| Python | `3.14.10` (versión usada en las pruebas locales) |
| Root Directory | raíz del repositorio |
| Branch | `main` |
| Build | `pip install -r requirements-api.txt` |
| Start | `uvicorn api.main:app --host 0.0.0.0 --port $PORT --workers 1 --limit-concurrency 32 --timeout-keep-alive 5` |
| Health Check | `/health` |
| Plan | `free` en el Blueprint; puede elegirse Starter |

Configuración basada en [FastAPI en Render](https://render.com/docs/deploy-fastapi),
[Blueprints](https://render.com/docs/blueprint-spec) y
[versión de Python](https://render.com/docs/python-version).

## Evitar builds del backend en Vercel

El `vercel.json` de la raíz establece `git.deploymentEnabled: false` para detener
despliegues automáticos Git de este backend, tanto de ramas como de `main`.
Esto no borra despliegues existentes ni bloquea despliegues manuales.
La conexión Git de un proyecto Vercel puede generar builds independientemente
de `render.yaml`; por eso un fallo de `vercel build` no es un despliegue Render.
[Configuración Git de Vercel](https://vercel.com/docs/project-configuration/git-configuration).

El frontend debe mantenerse en su propio proyecto/repositorio Vercel y consumir
la URL HTTPS del backend Render mediante `NEXT_PUBLIC_API_URL`. Este repositorio
no contiene una aplicación frontend Vercel. Para desvincularlo también en el
panel, usar Project Settings → Git → Disconnect en el proyecto Vercel del backend.

## Variables del backend

Variables:

- `APP_ENV=production`.
- `CORS_ORIGINS=https://tu-proyecto.vercel.app,https://preview-aprobado.vercel.app`.
  Sustituir por dominios reales. Sin barra final, rutas, comodines ni credenciales.
  Se pueden incluir dominios personalizados exactos HTTPS. Una variable vacía
  no permite ningún origen de navegador; la API y `/health` siguen funcionando.
- `PROMETHEUS_DATASET_PATH`: opcional, ruta de un JSON reducido validado, por
  ejemplo un Secret File de Render `/etc/secrets/descriptors.json`.
  No apunta a una URL y no habilita automáticamente el modo externo.

El Blueprint solicita `CORS_ORIGINS` durante su configuración (`sync: false`).
Cada preview Vercel debe aprobarse mediante su origen exacto; no usar regex para
todos los subdominios `.vercel.app`. No se permiten cookies/credentials CORS.
Los métodos permitidos son GET/POST y la cabecera de petición Content-Type.
Comportamiento conforme a [CORS de FastAPI](https://fastapi.tiangolo.com/tutorial/cors/).

CORS controla el acceso desde navegadores; no es autenticación ni impide llamadas
desde curl/servidores. Antes de habilitar consultas científicas con cuota o coste,
añadir autenticación y rate limiting en el gateway. La entrega actual no ejecuta
consultas científicas facturables.

## Comprobación tras desplegar

1. `GET /health`: 200, `default_data_mode=demo`, `live_sources_ready=false`.
2. Abrir `/docs`, ejecutar el ejemplo de análisis y verificar Top 3 + GeoJSON.
3. Ejecutar `OPTIONS /api/v1/analyze` con Origin del frontend aprobado y
   Access-Control-Request-Method: POST; comprobar el origen exacto en respuesta.
4. Un origen ajeno no recibe Access-Control-Allow-Origin. Un error 422/503 desde
   el origen aprobado sí debe ser legible por el frontend.
5. Solicitar `external` sin configurar datos: 503 explícito. La demo sigue
   disponible por petición, aunque el archivo externo esté ausente o inválido.

No instalar geopandas/rasterio/GDAL en el servicio por anticipación. Los cálculos
actuales operan sobre unos pocos descriptores. Procesamiento pesado y generación
de COG viven fuera de Render; los workers geoespaciales tendrán dependencias y
recursos propios. No se verificó un despliegue real de Render/Vercel en esta entrega.

## Validación local

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m pip check
```

La suite incluye regresión del núcleo/Streamlit, equivalencia del ranking,
pesos, Top 3, GeoJSON, Luna externa, nulos/nodata, CORS exacto, límites y timeouts.
Los transportes se prueban con respuestas simuladas; no equivalen a una consulta
real a NASA, ESA, JAXA o CSA. `docs/VERIFICATION_API.md` registra la ejecución.
