# Cuentas, sesiones y administración de usuarios

Cuentas de usuario sobre PostgreSQL (SQLAlchemy 2 + psycopg 3): inicio de sesión
con token *bearer*, alta por contraseña temporal y CRUD de usuarios para el
administrador. Todas las rutas están bajo `/api/v1`, intercambian JSON en
`snake_case` y requieren `Content-Type: application/json` cuando llevan cuerpo.

Las rutas de análisis (`/api/v1/analyze`, `/api/v1/sources`,
`/api/v1/earth-engine/locations`) siguen siendo públicas y no cambian. Si
`DATABASE_URL` no está configurada, todas las rutas de este documento responden
503 `auth_not_configured` y el resto de la API funciona igual.

```text
api/db.py                 motor SQLAlchemy, normalización de DATABASE_URL
api/models.py             tablas users y auth_sessions
api/security.py           scrypt, tokens, contraseñas temporales, límite de intentos
api/account_schemas.py    modelos Pydantic de petición y respuesta
api/services/accounts.py  casos de uso y reglas
api/routers/accounts.py   rutas /auth y /users
api/manage.py             comandos de operación (init-db, admin)
```

## Flujo de alta

1. El administrador crea la cuenta con `POST /users` indicando solo el correo.
   La respuesta incluye la contraseña temporal: **es la única vez que se
   devuelve** y hay que entregarla a la agencia por un canal aparte.
2. La agencia inicia sesión con esa contraseña (`POST /auth/login`). Su usuario
   está en estado `pendiente`: solo puede llamar a `GET /auth/me`,
   `POST /auth/logout` y `POST /auth/registration`; las demás rutas de cuentas
   responden 403.
3. Completa su registro con `POST /auth/registration`: contraseña propia y
   perfil. La cuenta pasa a `activo`, se guarda `registered_at`, se borra
   `temporary_expires_at` y **se cierran todas sus sesiones**.
4. Inicia sesión de nuevo con su contraseña propia. La temporal deja de servir.

La contraseña temporal vence a los 7 días (`TEMPORARY_PASSWORD_DAYS`). Si vence
o la agencia olvida su contraseña, el administrador emite otra con
`POST /users/{id}/temporary-password`: la cuenta vuelve a `pendiente`, conserva
su perfil, se cierran sus sesiones y debe repetir el paso 3.

## Autenticación

`POST /auth/login` devuelve un token opaco. El cliente lo envía en cada petición:

```http
Authorization: Bearer <token>
```

No hay cookies (`allow_credentials=False` en CORS), así que no aplica CSRF. La
sesión dura 12 horas (`SESSION_HOURS`) desde el inicio de sesión y no se
renueva con el uso; al vencer, el cliente debe iniciar sesión otra vez. La
sesión de un usuario `pendiente` nunca dura más que su contraseña temporal.

## Objeto usuario

Es el mismo en todas las respuestas y nunca incluye hashes ni contraseñas:

```json
{
  "id": "0b0f7c7e-7f0a-4c58-9d63-1f1f4f0c7a11",
  "email": "agencia@example.org",
  "role": "agencia",
  "status": "activo",
  "profile": {"full_name": "Ana Pérez", "position": "", "agency": "Agencia TEST", "acronym": "", "country": "Chile"},
  "created_at": "2026-11-14T18:43:00.123Z",
  "registered_at": "2026-11-15T09:10:00.456Z",
  "temporary_expires_at": null
}
```

Recién creada, la misma cuenta se ve así: `"status": "pendiente"`,
`"profile": null`, `"registered_at": null` y `temporary_expires_at` con fecha.

- `role`: `admin` o `agencia`. `status`: `pendiente` o `activo`.
- `profile` es `null` mientras la cuenta no tenga datos de perfil: una agencia
  que aún no completa su registro o un administrador sin perfil. Cuando existe,
  trae siempre las cinco claves; los opcionales vacíos llegan como `""`.
- Las fechas son ISO-8601 en UTC con milisegundos y sufijo `Z`, el formato de
  `Date.prototype.toISOString()`. `registered_at` y `temporary_expires_at`
  pueden ser `null`.
- `temporary_expires_at` solo tiene valor mientras hay una contraseña temporal
  vigente o vencida sin usar.

## Rutas

| Método y ruta | Quién | Cuerpo | Éxito |
|---|---|---|---|
| `POST /auth/login` | cualquiera | `{"email","password"}` | 200 `{"token","expires_at","user"}` |
| `POST /auth/logout` | cualquier sesión | – | 204 sin cuerpo; borra esa sesión |
| `GET /auth/me` | cualquier sesión | – | 200 usuario |
| `POST /auth/registration` | sesión de un usuario `pendiente` | `{"password","profile":{…}}` | 204 sin cuerpo |
| `GET /users` | admin | – | 200 `{"users":[usuario,…]}` por `created_at` |
| `POST /users` | admin | `{"email","temporary_password"?}` | 201 `{"user","temporary_password"}` |
| `GET /users/{id}` | admin | – | 200 usuario |
| `PATCH /users/{id}` | admin | `{"email"?,"profile"?}` | 200 usuario |
| `DELETE /users/{id}` | admin | – | 204 sin cuerpo |
| `POST /users/{id}/temporary-password` | admin | `{"temporary_password"?}` u omitido | 200 `{"user","temporary_password"}` |

Detalles por ruta:

- **Login.** El correo se recorta y pasa a minúsculas; la contraseña se compara
  tal cual, sin recortar. Correo desconocido y contraseña incorrecta devuelven
  exactamente la misma respuesta 401 y tardan lo mismo. `expires_at` es el
  vencimiento de la sesión. Una contraseña temporal vencida responde 401
  `temporary_password_expired` solo si la contraseña era la correcta.
- **Registration.** `profile` lleva `full_name`, `position`, `agency`,
  `acronym` y `country`. Tras el 204 el token usado deja de ser válido.
- **POST /users.** Crea rol `agencia` en estado `pendiente`. Si
  `temporary_password` se omite o es `null`, el servidor genera una con formato
  `XXXX-XXXX-XXXX` (mayúsculas y dígitos sin `0`, `O`, `1`, `I`). Si se envía,
  se guarda exactamente como llega.
- **PATCH.** Al menos uno de `email` o `profile`. `profile` reemplaza el perfil
  completo y sigue las reglas del registro. Cambiar el correo no cierra las
  sesiones del usuario. No se pueden cambiar rol, estado ni contraseña.
- **DELETE.** Borra el usuario y sus sesiones. Un administrador no puede
  borrarse a sí mismo y nunca se borra al último administrador.
- **temporary-password.** Solo para rol `agencia`. El cuerpo es opcional.

## Validación

| Campo | Regla |
|---|---|
| `email` | Se recorta y pasa a minúsculas. Un solo `@`, dominio con punto, sin espacios, máximo 254 caracteres. Único sin distinguir mayúsculas |
| `password`, `temporary_password` | Entre 8 y 128 caracteres. La contraseña propia debe ser distinta de la temporal |
| `profile.full_name`, `profile.agency`, `profile.country` | Obligatorios tras recortar; máximo 120 caracteres |
| `profile.position` | Opcional; máximo 80 |
| `profile.acronym` | Opcional; máximo 20 |

Los campos desconocidos, los tipos incorrectos, el texto de perfil que supera su
longitud máxima o contiene caracteres de control y el JSON mal formado
responden 422 `invalid_request`. Los opcionales del perfil pueden omitirse o
enviarse como `""` o `null`.

## Errores

Todos usan la forma común `{"detail": {"code": "...", "message": "..."}}`, con
`message` en español. El cliente debe decidir por `code`.

| Código | HTTP | Cuándo |
|---|---|---|
| `auth_not_configured` | 503 | Falta `DATABASE_URL`; afecta a todas las rutas `/auth` y `/users` |
| `database_unavailable` | 503 | La base de datos no responde; sin detalles |
| `invalid_credentials` | 401 | Correo desconocido o contraseña incorrecta, mismo mensaje |
| `temporary_password_expired` | 401 | Contraseña temporal correcta pero vencida |
| `too_many_attempts` | 429 | Cinco intentos fallidos para el mismo correo e IP en 15 minutos |
| `not_authenticated` | 401 | Token ausente, mal formado, desconocido o de una sesión ya cerrada |
| `session_expired` | 401 | La sesión venció |
| `forbidden` | 403 | No es administrador, o es un usuario `pendiente` fuera de sus tres rutas |
| `not_pending` | 409 | Registro de una cuenta que ya está activa |
| `invalid_email` | 422 | Correo con formato inválido |
| `email_exists` | 409 | Ya hay una cuenta con ese correo |
| `password_too_short` | 422 | Menos de 8 caracteres |
| `password_too_long` | 422 | Más de 128 caracteres |
| `password_same_as_temporary` | 422 | La contraseña propia es igual a la temporal |
| `incomplete_profile` | 422 | Falta `full_name`, `agency` o `country` |
| `user_not_found` | 404 | El `id` no existe o no es un UUID |
| `cannot_delete_self` | 409 | El administrador intenta borrarse |
| `last_admin` | 409 | Se borraría al último administrador |
| `not_allowed_for_admin` | 409 | Contraseña temporal para un administrador |
| `invalid_request` | 422 | Cuerpo mal formado: JSON inválido o ilegible, tipos incorrectos, campos ausentes o desconocidos |

Orden de comprobación: configuración y base de datos (503), sesión (401),
permiso (403) y después el cuerpo y las reglas (404, 409, 422). Un JSON
sintácticamente inválido responde 422 antes que todo lo demás. Las respuestas
401 llevan la cabecera `WWW-Authenticate: Bearer`. Una ruta o un método que no
existe responde el 404 o 405 por defecto del framework (`{"detail": "Not Found"}`),
igual que en el resto de la API.

Con las rutas actuales `last_admin` no puede ocurrir por HTTP: quien borra es
otro administrador, y borrarse a uno mismo responde antes `cannot_delete_self`.
Es una salvaguarda del servicio frente a peticiones simultáneas.

Límite de intentos: tras cinco fallos para el mismo par (correo, IP del
cliente) en 15 minutos, el login responde 429 hasta que el fallo más antiguo
sale de la ventana, incluso con la contraseña correcta. Un inicio de sesión
correcto borra el contador.

## Ejemplos

```bash
curl -sS http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.org","password":"<contraseña>"}'

curl -sS http://localhost:8000/api/v1/users \
  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  -d '{"email":"agencia@example.org"}'
```

```javascript
const api = import.meta.env.VITE_API_BASE_URL;
const response = await fetch(`${api}/api/v1/auth/registration`, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
  body: JSON.stringify({
    password,
    profile: { full_name, position, agency, acronym, country },
  }),
});
if (response.status !== 204) {
  const { detail } = await response.json();
  throw new Error(detail.message); // decidir por detail.code
}
// 204: descartar el token y pedir inicio de sesión con la contraseña nueva.
```

## Configuración

| Variable | Valor por defecto | Uso |
|---|---|---|
| `DATABASE_URL` | – | URL de PostgreSQL. Se aceptan `postgres://` y `postgresql://`; se usa siempre el driver psycopg 3 |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | – | Primer administrador; se crea al arrancar solo si no existe ningún administrador |
| `SESSION_HOURS` | `12` | Duración de la sesión |
| `TEMPORARY_PASSWORD_DAYS` | `7` | Vigencia de una contraseña temporal |

La aplicación falla al arrancar, sin mostrar valores, si `ADMIN_EMAIL` no es un
correo, si `ADMIN_PASSWORD` no tiene entre 8 y 128 caracteres o si las
duraciones no son positivas. Si la base de datos no responde al arrancar, la
API arranca igualmente: las cuentas responden 503 `database_unavailable` y la
creación de tablas y del administrador se reintenta en peticiones posteriores.
`GET /health` incluye `accounts_configured` sin consultar la base de datos.

Con Neon, usar la cadena de conexión *pooled*. Ese extremo es PgBouncer en modo
transacción, por lo que el motor desactiva las sentencias preparadas del
servidor (`prepare_threshold=None`) y usa un pool pequeño (3 conexiones más 2
de desbordamiento, comprobación previa y reciclado a los 5 minutos). La
conexión se abandona a los 10 segundos salvo que la URL indique otro
`connect_timeout`.

## Ejecución local

La aplicación no carga archivos `.env` por sí misma. Para desarrollo,
`requirements-dev.txt` incluye `python-dotenv`, que permite a Uvicorn leer el
archivo con `--env-file`. El `.env` debe tener líneas `CLAVE=VALOR` sin
comillas ni `export`; partir de `.env.example`.

Uvicorn (python-dotenv) y `api.manage` leen ese archivo con reglas distintas.
Coinciden mientras los valores no empiecen por comillas ni contengan ` #`
(espacio y almohadilla) o `${...}`: python-dotenv quita las comillas, corta en
` #` y expande `${NOMBRE}`; `api.manage` conserva el valor tal cual. Una
`ADMIN_PASSWORD` con esos caracteres se leería distinta en cada herramienta.

En Windows, una vez creado `.venv` y completado `.env`, `iniciar-api.cmd` hace el
último paso con doble clic: arranca Uvicorn con el Python de `.venv` y lee `.env`.
Hay que usar ese Python: el de otros entornos no tiene SQLAlchemy ni psycopg.

PowerShell (Windows):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m api.manage --env-file .env admin
.venv\Scripts\python -m uvicorn api.main:app --env-file .env --host 127.0.0.1 --port 8000
```

bash (macOS, Linux, Git Bash):

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m api.manage --env-file .env admin
uvicorn api.main:app --env-file .env --host 127.0.0.1 --port 8000
```

Sin `python-dotenv` (por ejemplo con solo `requirements-api.txt`), exportar las
variables en la terminal antes de arrancar:

```powershell
Get-Content .env | Where-Object { $_ -match '^\s*[^#\s][^=]*=' } | ForEach-Object {
  $name, $value = $_ -split '=', 2
  Set-Item -Path "Env:$($name.Trim())" -Value $value.Trim()
}
.venv\Scripts\python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

```bash
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|\#*) continue ;; esac
  export "${line%%=*}=${line#*=}"
done < .env
uvicorn api.main:app --host 127.0.0.1 --port 8000
```

No usar `source .env`: la URL de la base de datos contiene `&`, que el shell
interpreta. Documentación interactiva en `http://localhost:8000/docs`, con el
botón **Authorize** para pegar el token.

## Administrador y comandos

```bash
python -m api.manage --env-file .env init-db        # crea las tablas si no existen
python -m api.manage --env-file .env admin          # crea el administrador si no existe
python -m api.manage --env-file .env admin --reset  # además aplica ADMIN_PASSWORD y cierra sus sesiones
```

`--env-file` es opcional y va antes del comando; lee líneas `CLAVE=VALOR` y
comentarios `#`, sin comillas ni interpolación, y no sustituye variables que ya
existan en el entorno. Los comandos solo imprimen líneas de estado, nunca
contraseñas ni la URL.

- Al arrancar la API ocurre lo mismo que con `init-db` y `admin` sin `--reset`,
  con una diferencia: el arranque solo crea el administrador cuando **no existe
  ninguno**. Nunca modifica la contraseña de uno existente, aunque
  `ADMIN_PASSWORD` cambie.
- `admin` crea la cuenta de `ADMIN_EMAIL` si no existe, aunque haya otros
  administradores. Si ese correo pertenece a una agencia, no la modifica y
  termina con código 1.
- Para cambiar la contraseña del administrador: actualizar `ADMIN_PASSWORD` y
  ejecutar `admin --reset`.
- Creado el administrador, `ADMIN_PASSWORD` puede retirarse del entorno.

## Base de datos

`users`: `id` (UUID4 como texto), `email` único en minúsculas, `role`,
`status`, `password_hash`, `temporary_expires_at`, columnas de perfil
(`full_name`, `position`, `agency`, `acronym`, `country`), `created_at`,
`updated_at`, `registered_at`.

`auth_sessions`: `id`, `user_id` (clave foránea con `ON DELETE CASCADE`,
indexada), `token_hash` único, `created_at`, `expires_at`, `last_used_at`.
`last_used_at` se actualiza como mucho una vez por minuto y sesión. Las
sesiones vencidas de un usuario se eliminan en su siguiente inicio de sesión;
hasta entonces su token responde `session_expired`.

Todas las fechas son `timestamp with time zone` en UTC. No hay herramienta de
migraciones: `create_all` crea las tablas que faltan y no altera las
existentes. SQLite se admite solo para las pruebas.

## Seguridad

- **Contraseñas.** `hashlib.scrypt` con `n=2**15`, `r=8`, `p=3`, sal aleatoria
  de 16 bytes y clave de 32 bytes; unos 32 MiB por cálculo. Formato guardado:
  `scrypt$15$8$3$<sal base64>$<clave base64>`. Los parámetros viajan con cada
  hash, de modo que pueden endurecerse sin invalidar los existentes. La
  comparación usa `hmac.compare_digest`. Como mucho se calculan dos hashes a
  la vez para acotar la memoria; el resto espera. Cada cálculo tarda del orden
  de un segundo en un portátil y más en una instancia pequeña.
- **Tokens de sesión.** 32 bytes aleatorios (`secrets.token_urlsafe`). En la
  base de datos solo se guarda su SHA-256: una copia de la tabla no sirve para
  suplantar sesiones. El token solo aparece en la respuesta del login.
- **Token en el frontend.** Lo guarda el cliente. `sessionStorage` o memoria
  limitan su vida a la pestaña; `localStorage` lo expone durante más tiempo a
  cualquier script inyectado (XSS). En ambos casos conviene borrarlo al
  recibir 401 y al cerrar sesión.
- **Contraseñas temporales.** 12 caracteres de un alfabeto de 32 símbolos (60
  bits), generados con `secrets`. Se devuelven una sola vez y se guardan solo
  como hash.
- **Límite de intentos.** En memoria y por proceso, con un máximo de 10 000
  claves. Detrás del proxy de Render, Uvicorn ve la IP del proxy salvo que se
  configure `--forwarded-allow-ips`, así que en la práctica el límite se
  aplica por correo: frena la fuerza bruta, pero permite que un tercero
  bloquee temporalmente el login de un correo conocido.
- **Cambios simultáneos.** El hash se calcula fuera de toda transacción. Al
  terminar, login, registro y restablecimiento releen la cuenta con bloqueo de
  fila y escriben en una sola transacción: un login en curso no sobrevive a un
  restablecimiento o registro que terminó antes, y una petición abandonada por
  el límite de tiempo mientras calculaba su hash ya no escribe nada.
- **Respuestas.** Nunca incluyen hashes, la URL de la base de datos ni
  mensajes del driver. Las respuestas correctas con cuerpo llevan
  `Cache-Control: no-store`. Los registros del servidor anotan solo la clase
  del error de base de datos y su SQLSTATE.

Pendiente antes de un uso en producción:

- **Solo HTTPS.** Tokens y contraseñas viajan en la petición. Render termina
  TLS, pero la aplicación no redirige ni rechaza HTTP por sí misma.
- **Entrega de contraseñas temporales por correo.** Hoy el administrador las ve
  en pantalla y las comunica a mano.
- **Límite de intentos compartido.** Con más de un worker o instancia, cada
  proceso cuenta por separado; haría falta un almacén común (por ejemplo
  Redis) o un límite en el gateway. Tampoco hay un tope global de peticiones
  al login: muchos correos distintos siguen costando un hash cada uno.
- **Migraciones.** Cualquier cambio de esquema requiere hoy SQL manual; falta
  una herramienta como Alembic.
- No existe cambio de contraseña por el propio usuario ni recuperación por
  correo: el administrador emite una contraseña temporal nueva.

## Pruebas

```bash
python -m pytest -q
```

`tests/test_accounts.py` usa SQLite en memoria mediante el motor inyectado
(`create_app(settings, engine=...)`): no necesita PostgreSQL ni red. Aunque
`DATABASE_URL` esté exportada en la terminal, las pruebas no abren ninguna
conexión con ella. Las pruebas reducen el coste de scrypt; una de ellas ejecuta
los parámetros de producción. Varias intercalan dos peticiones sobre la misma
cuenta mientras una de ellas calcula su hash.
