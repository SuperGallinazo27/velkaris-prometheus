# Colaborar en VELKARIS / PROMETHEUS

Cada integrante usa su propia cuenta de GitHub. El propietario invita a los cinco colaboradores; nadie necesita compartir contraseñas o tokens.

## Flujo de trabajo

1. Actualiza tu copia de `main` y crea una rama corta, por ejemplo `feat/mapa`, `fix/login` o `docs/guia`.
2. Realiza un cambio concreto y ejecuta las comprobaciones indicadas abajo.
3. Publica tu rama y abre un pull request hacia `main`. Explica el cambio y cómo lo validaste.
4. Solicita revisión a otro integrante con acceso al repositorio. Resuelve sus observaciones y espera a que las comprobaciones automáticas pasen.
5. Fusiona el PR aprobado y elimina su rama cuando ya no se necesite. No subas cambios directamente a `main`.

Si todavía no aceptaste la invitación, puedes preparar cambios en un fork del repositorio público y abrir un PR. La revisión y los permisos de ejecución siguen siendo necesarios.

## Comprobaciones

Sigue primero los requisitos de instalación del README.

```sh
python -m pip install -r requirements-dev.txt
python -m pip check
python -m pytest -q
```

## Despliegues y acceso

El backend se aloja en Render. El acceso a GitHub no concede acceso al panel de alojamiento. En los planes Hobby actuales, el propietario conserva la administración del alojamiento; no se deben activar pruebas Pro ni contratar asientos para colaborar.

Los cambios de código pasan por PR. Antes de dar una entrega por terminada, comprueba el estado del despliegue asociado en GitHub y solicita al propietario verificar el panel si hace falta. Un PR fusionado o unas pruebas correctas no garantizan que la publicación haya terminado.

## Credenciales y datos

No incluyas secretos, archivos `.env`, credenciales, datos personales ni valores sensibles en commits, issues, PR o capturas. Usa únicamente los ejemplos de configuración del repositorio para desarrollo local. Los cambios de variables o credenciales de producción corresponden al propietario.

Las reglas de protección se administran en Settings → Branches/Rules; esta guía describe el proceso y no demuestra por sí sola que una regla esté activada.
