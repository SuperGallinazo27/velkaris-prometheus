# Conectar datos reales

El MVP consume descriptores JSON, no archivos de elevación directamente. `demo.json`
es una muestra exclusivamente sintética; no debe reutilizarse como dato científico.

Para conectar una fuente: extraer descriptores de sus rásteres en un proceso aparte,
crear un JSON con la misma estructura y cargarlo en **Importar descriptores**.
`prometheus/data.py` es el punto de entrada y validación reutilizable por un futuro
adaptador NASA/PDS/USGS. No hay ninguna descarga científica implementada.

- `schema_version`: 1; `mode`: `external`; `description`: alcance y limitaciones.
- `mars`: una o más regiones; `earth`: exactamente tres candidatos.
- Cada región: `id` único, `name`, `relief_m`, `slope_deg`, `roughness_m`.
- Cada candidato terrestre añade `lat` y `lon` en grados WGS84, longitud de −180 a 180.
- En modo `external`, **cada registro** exige además `source` (URL o cita del conjunto),
  `product_id`, `method`, `resolution_m`, `window_m` y `reference_frame`.
  Los dos tamaños deben ser positivos, en metros; la ventana no puede ser menor
  que la resolución. `reference_frame` debe documentar referencias horizontal y vertical.

Definiciones: relieve = máximo menos mínimo de elevación en la ventana;
pendiente = promedio de pendientes locales en grados; rugosidad = RMS de
residuales de elevación después de retirar un plano local, en metros.
Documentar algoritmo, tratamiento de NoData y cobertura válida en `method`.

Antes de comparar, armonizar resolución, ventana y procesamiento entre planetas;
proyectar con unidades métricas apropiadas a cada cuerpo y registrar incertidumbres.
El importador valida estructura y rangos básicos, **no** autenticidad, calidad,
compatibilidad de métodos ni validez científica. Las escalas del ranking siguen
siendo ilustrativas incluso con datos importados; deben justificarse/calibrarse.
No se incluyen plantillas marcadas como reales con cifras inventadas.
