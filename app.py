from pathlib import Path
import csv
import io

import streamlit as st

from prometheus.data import FEATURES, load_dataset
from prometheus.ranking import rank_candidates

ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title="PROMETHEUS · VELKARIS", page_icon="🪐", layout="wide")
st.caption("VELKARIS / PLANETARY ANALOG FINDER")
st.title("PROMETHEUS")
st.write("Explora diferencias topográficas entre una región de Marte y tres candidatos terrestres.")

with st.sidebar:
    st.header("Explorar terrenos")
    mode = st.radio("Origen de datos", ["Demostración", "Importar descriptores"])
    st.caption("MVP local · Sin claves API · Sin IA generativa")

try:
    if mode == "Demostración":
        data = load_dataset(ROOT / "data" / "demo.json")
    else:
        uploaded = st.sidebar.file_uploader("Archivo JSON", type=["json"])
        st.sidebar.caption("Formato y campos de procedencia: data/README.md")
        if uploaded is None:
            st.info("Carga descriptores topográficos en JSON. Todavía no hay datos científicos descargados.")
            st.stop()
        data = load_dataset(uploaded)
except (ValueError, OSError) as exc:
    st.error(f"No se puede usar este archivo: {exc}")
    st.stop()

demo = data["mode"] == "demo"
status = "DEMOSTRACIÓN" if demo else "IMPORTADO · NO VALIDADO"
if demo:
    st.warning("DEMOSTRACIÓN: todos los valores topográficos son sintéticos. No son mediciones NASA ni prueban que estos lugares sean análogos de Marte. Los puntos del mapa son orientativos.")
else:
    st.warning("DATOS IMPORTADOS: la procedencia es declarada por quien aporta el archivo. PROMETHEUS no verifica su autenticidad ni su comparabilidad científica.")
st.caption(data["description"])

regions = {region["id"]: region for region in data["mars"]}
selected = st.sidebar.selectbox("Región marciana", list(regions), format_func=lambda key: regions[key]["name"])
target = regions[selected]
st.subheader(target["name"])
for column, (key, (label, _)) in zip(st.columns(3), FEATURES.items()):
    column.metric(label, f"{target[key]:g}")
st.caption(f"Valores de referencia · {status}")

results = rank_candidates(target, data["earth"])
st.subheader("Ranking exploratorio")
st.write("Menor distancia significa menor diferencia en los tres descriptores usados. No es una probabilidad ni un porcentaje de similitud.")
rows = []
previous = None
position = 0
for index, result in enumerate(results, start=1):
    if result["distance"] != previous:
        position = index
    previous = result["distance"]
    site = result["site"]
    rows.append({
        "Posición": position, "Candidato": site["name"],
        "Distancia (sin unidad)": result["distance"],
        **{label: site[key] for key, (label, _) in FEATURES.items()},
        "Latitud": site["lat"], "Longitud": site["lon"],
    })
st.dataframe(rows, hide_index=True, width="stretch",
             column_config={"Distancia (sin unidad)": st.column_config.NumberColumn(format="%.4f")})

map_column, details_column = st.columns([1.25, 1])
with map_column:
    st.subheader("Candidatos en la Tierra")
    st.map([{"lat": r["site"]["lat"], "lon": r["site"]["lon"]} for r in results], size=45000)
    st.caption("Mapa terrestre, sin puntos marcianos. El fondo cartográfico necesita internet; la tabla conserva las coordenadas.")
with details_column:
    st.subheader("Comparación con Marte")
    chosen = st.selectbox("Examinar candidato", [r["site"]["id"] for r in results],
                          format_func=lambda key: next(r["site"]["name"] for r in results if r["site"]["id"] == key))
    result = next(r for r in results if r["site"]["id"] == chosen)
    st.dataframe([
        {"Descriptor": label, "Marte": target[key], "Tierra": result["site"][key],
         "Diferencia absoluta": result["differences"][key]}
        for key, (label, _) in FEATURES.items()
    ], hide_index=True, width="stretch")
    st.caption("Relieve = elevación máxima menos mínima en una ventana. Rugosidad = RMS de alturas residuales tras retirar un plano local. Estas definiciones deben mantenerse al incorporar datos.")

with st.expander("Cómo se calcula y qué falta validar"):
    st.code("distancia = (|Δ relieve| / 1000 m + |Δ pendiente| / 15° + |Δ rugosidad| / 100 m) / 3", language=None)
    st.write("Los tres descriptores tienen el mismo peso. Las escalas son decisiones ilustrativas del prototipo, no umbrales científicos. La distancia puede superar 1. Los empates exactos comparten posición; el orden alfabético solo organiza su presentación.")
    st.write("Se usa relieve local, no elevación absoluta, porque Marte y la Tierra tienen referencias verticales distintas. Antes de interpretar resultados hay que armonizar resolución, tamaño de ventana, proyección, método, huecos y calidad de los datos. No se comparan geología, clima, gravedad ni seguridad de acceso.")
    st.write("El ranking solo ordena estos tres ejemplos; no busca nuevos lugares a escala planetaria ni acredita su idoneidad para pruebas de robots.")
with st.expander("Datos y procedencia"):
    st.json(data)

output = io.StringIO()
export = [{"Estado": status, "Región marciana": target["name"], **row} for row in rows]
writer = csv.DictWriter(output, fieldnames=list(export[0]))
writer.writeheader()
writer.writerows(export)
st.download_button("Descargar ranking CSV", output.getvalue().encode("utf-8-sig"),
                   file_name="prometheus_ranking_demo.csv" if demo else "prometheus_ranking_importado.csv", mime="text/csv")
