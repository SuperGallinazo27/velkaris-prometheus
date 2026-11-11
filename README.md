# VELKARIS — PROMETHEUS

MVP local en Python + Streamlit: selección de tres regiones marcianas, comparación
topográfica con tres candidatos terrestres, mapa, ranking y exportación CSV.
**Todos los datos incluidos son demostrativos y sintéticos.** No contiene datos
NASA, porcentajes de similitud, IA entrenada ni validación científica.

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
Solo Streamlit es dependencia directa; instala sus propias dependencias.
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
python -m unittest discover -s tests -v
```

Las pruebas de Streamlit simulan interacciones; no verifican visualmente las teselas
del proveedor de mapas. Consulta `VERIFICACION.md` para el resultado de esta entrega.

Referencias técnicas: [ejecución de Streamlit](https://docs.streamlit.io/develop/api-reference/cli/run),
[mapa](https://docs.streamlit.io/develop/api-reference/charts/st.map) y
[AppTest](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest).
