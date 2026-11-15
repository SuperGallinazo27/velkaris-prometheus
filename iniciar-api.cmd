@echo off
rem Enciende la API de PROMETHEUS (cuentas y analisis) en http://127.0.0.1:8000
rem Usa el Python de esta carpeta (.venv) y la configuracion privada (.env).
rem No es la aplicacion Streamlit (app.py): el frontend necesita esta API.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Falta el entorno .venv de esta carpeta. Pasos en docs\ACCOUNTS.md, "Ejecucion local".
  pause
  exit /b 1
)
if not exist ".env" (
  echo Falta el archivo .env con DATABASE_URL, ADMIN_EMAIL y ADMIN_PASSWORD. Copia .env.example y completalo.
  pause
  exit /b 1
)
echo API de PROMETHEUS en http://127.0.0.1:8000  (documentacion en http://127.0.0.1:8000/docs)
echo Deja esta ventana abierta mientras uses la aplicacion. Para apagarla: Ctrl+C.
".venv\Scripts\python.exe" -m uvicorn api.main:app --env-file .env --host 127.0.0.1 --port 8000
pause
