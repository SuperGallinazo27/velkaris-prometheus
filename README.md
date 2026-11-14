# VELKARIS — PROMETHEUS

MVP local en Python + Streamlit: selección de tres regiones marcianas, comparación
topográfica con tres candidatos terrestres, mapa, ranking y exportación CSV.
**Todos los datos incluidos son demostrativos y sintéticos.** No contiene datos
NASA, porcentajes de similitud, IA entrenada ni validación científica.

También incluye una **API REST FastAPI modular**, preparada para Render y un frontend
Vercel. `POST /api/v1/analyze` entrega Top 3, GeoJSON y procedencia; la demo funciona
sin cuentas externas. La lógica original de `prometheus/data.py` y
`prometheus/ranking.py` se conserva. Los conectores por agencia son contratos
explícitos pendientes de extracción por producto, no integraciones en vivo terminadas.

## API: inicio rápido

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-api.txt
export CORS_ORIGINS=http://localhost:3000
uvicorn api.main:app --host 127.0.0.1 --port 8000
```

```bash
curl -sS http://localhost:8000/api/v1/analyze \
  -H 'Content-Type: application/json' \
  -d '{"mission_type":"mars","target_id":"jezero","data_mode":"demo","weights":{"relief_m":1,"slope_deg":1,"roughness_m":1}}'
```

Documentación interactiva: `http://localhost:8000/docs`; contrato OpenAPI:
`/openapi.json`; salud: `/health`; capacidades por fuente: `/api/v1/sources`.
`mission_type` acepta `mars` y `moon`, pero la demo existente solo tiene Marte.
No se inventó un dataset lunar ni información térmica.

- [Contrato, respuestas, datos externos y frontend](docs/API.md)
- [Render, variables de entorno y comprobación](docs/DEPLOYMENT.md)
- [Estrategia geoespacial, fuentes y límites](docs/DATA_STRATEGY.md)

`render.yaml` instala solo `requirements-api.txt` para evitar Streamlit, pandas,
GDAL o rásteres en el servicio web. La configuración CORS acepta únicamente
orígenes exactos declarados en `CORS_ORIGINS`; vacía no habilita ningún origen.
No se permite `*`, tampoco para previews Vercel.

## Ejecutar en Mac y VS Code

Requiere Python 3.11 o posterior. Abre esta carpeta en VS Code y ejecuta en su terminal:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Abre http://localhost:8501. Para detener: Ctrl+C. Para abrir de nuevo, ejecuta las
líneas de activación e inicio. En VS Code selecciona `.venv/bin/python` como intérprete;
también está incluida la tarea **Iniciar PROMETHEUS** (Terminal → Ejecutar tarea).
`requirements.txt` incluye Streamlit y la API; cada interfaz tiene su comando de inicio.
La instalación y el fondo del mapa necesitan internet. El cálculo usa archivos locales,
no requiere cuentas, claves API ni servicios de pago.

## Archivos

- `app.py`: interfaz, mapa y exportación.
- `prometheus/data.py`: carga y validación de datos.
- `prometheus/ranking.py`: distancia transparente entre descriptores.
- `data/demo.json`: ejemplos sintéticos.
- `data/README.md`: contrato para conectar descriptores científicos reales.
- `tests/`: comprobaciones de ranking, datos e interacción de la app.

La distancia es el promedio de diferencias absolutas escaladas: relieve / 1000 m,
pendiente / 15° y rugosidad / 100 m. Menor es más próximo en esos descriptores;
no es probabilidad. Pesos y escalas son ilustrativos. Los empates comparten posición.
No se compara elevación absoluta entre planetas, ni geología, clima o accesibilidad.

## Verificar

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
# Solo las pruebas originales (unittest):
python -m unittest discover -s tests -v
```

Las pruebas de Streamlit simulan interacciones; no verifican visualmente las teselas
del proveedor de mapas. Consulta `VERIFICACION.md` para el resultado de esta entrega.

Referencias técnicas: [ejecución de Streamlit](https://docs.streamlit.io/develop/api-reference/cli/run),
[mapa](https://docs.streamlit.io/develop/api-reference/charts/st.map) y
[AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest).
