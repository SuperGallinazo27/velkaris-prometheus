# Verificación de la API — 2026-11-14

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
