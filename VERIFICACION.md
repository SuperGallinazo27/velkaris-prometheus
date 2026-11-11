# Verificación de la entrega

Fecha: 11 de noviembre de 2026.

- Entorno observado: Mac, Python 3.14.10, Streamlit 1.67.0.
- Instalación aislada y comprobación de dependencias: sin incompatibilidades.
- `python -m unittest discover -s tests -v`: **12 pruebas aprobadas**.
- Se verificaron fórmula, orden del ranking, empates, cambio de región y candidato,
  ausencia de resultados sin archivo importado, rechazo de datos inválidos y
  exigencia de procedencia para el contrato de importación.
- Servidor local iniciado; endpoint de salud respondió `ok`.
- Revisión en navegador: interfaz cargada, aviso de demostración, tabla de ranking,
  comparación y fondo cartográfico con marcadores visibles.

La primera ejecución de las pruebas agotó el límite de 30 segundos durante la
carga inicial de dependencias. La repetición completa final pasó en 3,445 segundos.
El entorno emitió avisos sobre lectura restringida de información de caché de CPU;
no impidieron el cálculo ni el funcionamiento observado.

Límites: no se descargaron datos científicos, no se validó un análogo real y no se
probó una importación científica real. Las pruebas del contrato externo usan un
objeto sintético identificado como fixture de prueba. La revisión visual fue en
un navegador con ventana estrecha, no una auditoría de todos los dispositivos.
