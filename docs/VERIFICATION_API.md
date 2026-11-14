# Verificación de la API — 2026-11-14

## Ampliación Google Earth Engine

- SDK oficial instalado: `earthengine-api==1.7.52`, Python 3.14.10.
- Suite completa: **112 passed, 11 subtests passed** (4.76 s); `pip check` correcto.
- Se construyó y serializó el grafo NASADEM/MODIS usando el SDK oficial y sus
  definiciones de algoritmos de prueba, sin llamadas de red ni autenticación.
- Se comprobaron GeoJSON, metadatos, máscara/nulos en el contrato, límites de
  consultas, caché, método/resolución de objetivos, timeout, cancelación y tope
  de respuesta. Se ejecutó el worker real con una ruta inexistente para verificar
  el error de credenciales y que no exponga rutas ni claves.
- Las respuestas numéricas de las pruebas son fixtures TEST sintéticos; no son
  mediciones Google/NASA. No se comprobó acceso real a Earth Engine ni resultados
  geoespaciales contra observaciones de campo. El propietario configurará el
  proyecto y credenciales después en Render; GEE permanece desactivado por defecto.
- `prometheus/data.py`, `prometheus/ranking.py`, Streamlit y demo original intactos.
- Frontend separado inspeccionado: Vite con `VITE_API_BASE_URL`, todavía usa datos
  demo locales. No se modificó su repositorio en esta ampliación.
- En macOS Intel se instaló el SDK con `pip --only-binary=:all:` para elegir una
  rueda compatible de cryptography y evitar compilar Rust. No se modificó Python
  global. Persiste la advertencia de deprecación de httpx/TestClient ya registrada.

## Entrega inicial FastAPI

- Base inspeccionada: `main`, commit `4c6ba82c36324ddc51ea8b70a5994685324c143b`.
- Rama de trabajo: `feat/fastapi-render`; sin cambios en los archivos originales
  `prometheus/data.py`, `prometheus/ranking.py`, `app.py` ni `data/demo.json`.
- Python 3.14.10 en macOS Intel; dependencias directas fijadas en requirements.
- `python -m pytest -q`: **78 passed**, **11 subtests passed** (3.77 s).
  Incluye las 12 pruebas originales y las pruebas nuevas de API/transporte.
- `python -m pip check`: sin incompatibilidades de dependencias.
- `git diff --check`: sin errores de espacios.
- Proceso Uvicorn real local: `/health` 200; análisis demo 200 con tres
  resultados y CORS del origen aprobado; modo externo sin archivo 503.

La primera ejecución completa tuvo un timeout de arranque de 30 s en una prueba
original de Streamlit. La segunda ejecución completa pasó, sin modificar la
app original ni ampliar su timeout. Esto puede reaparecer en un arranque lento.
Starlette emitió una advertencia de deprecación del backend httpx de TestClient;
no afecta los resultados actuales, pero una actualización futura debe revisarla.

Cobertura funcional: igualdad con el cálculo original para las tres regiones
demo; pesos; errores 422/503; valores no finitos; nulos y nodata; Top 3 con más
candidatos y empates; coordenadas GeoJSON; datos lunares externos mediante fixture
sintético; indicadores térmicos ausentes/incompatibles/válidos; compatibilidad
v1; CORS exacto; límite de cuerpo incluso por fragmentos; límite de archivo;
deadline; fallos y límites del transporte JSON/STAC simulado.

No se probaron mediciones científicas reales, credenciales de agencias, extracción
GEE/COG ni un despliegue Render/Vercel. No se validó científicamente la distancia
ni su calibración: no existe porcentaje de similitud calibrado.
