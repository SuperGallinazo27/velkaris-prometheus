# Portal de turnos

Las agencias reservan un lugar análogo para un rango de días. El servidor guarda
cada turno en PostgreSQL, impide que dos agencias coincidan en el mismo lugar y
no deja reservar dentro de áreas de protección estricta. Todas las rutas están
bajo `/api/v1`, usan JSON en `snake_case` y requieren la sesión de una cuenta
que ya completó su registro (ver [ACCOUNTS.md](ACCOUNTS.md)).

Un turno coordina a las agencias de este portal. **No reemplaza los permisos**
del país, del dueño del terreno ni de la autoridad de un área protegida.

```text
data/lugares.json              catálogo de los 37 lugares con sus áreas protegidas
api/services/places.py         lectura del catálogo y regla de áreas protegidas
api/services/reservations.py   casos de uso: estado, disponibilidad, turnos, decisiones
api/reservation_schemas.py     modelos Pydantic de petición y respuesta
api/routers/reservations.py    rutas /places y /reservations
api/models.py                  tablas reservations y place_overrides
tests/test_reservations.py     47 pruebas
```

No hay variables de entorno nuevas. Las dos tablas se crean solas al arrancar,
igual que las de cuentas (`Base.metadata.create_all`); `python -m api.manage
init-db` también las crea.

## De dónde salen las áreas protegidas

De la **World Database on Protected and Conserved Areas (WDPCA)** de UNEP-WCMC y
la UICN, la base mundial que reúne las áreas que declaran los países y las de
convenios internacionales. El script `scripts/protegidas/calcular.py` del
repositorio del frontend consulta su servicio público de mapas para cada lugar y
escribe `data/lugares.json` (y la copia que muestra la aplicación). El servidor
decide con ese archivo: nunca confía en lo que diga el navegador.

- Cita obligatoria (está en `source.citation` del archivo y en `GET /places`):
  *UNEP-WCMC and IUCN (2026), Protected Planet: The World Database on Protected
  and Conserved Areas (WDPCA) [On-line], [October 2026], Cambridge, UK: UNEP-WCMC
  and IUCN. Available at: https://www.protectedplanet.net/.*
- [Condiciones de uso](https://www.protectedplanet.net/en/legal): uso no
  comercial; se puede publicar en línea si los datos no son descargables, la
  cita está a la vista y se indica el mes de la versión; no se puede
  redistribuir. El detalle y lo que eso implica para este repositorio están en
  el README del script.
- La base se publica cada mes. Para actualizarla, volver a ejecutar el script
  con `--todo` y desplegar.
- Puede estar incompleta o tener límites imprecisos. Un lugar sin polígono en su
  punto no garantiza que no haya un área protegida.

## La regla

Es un **criterio de PROMETHEUS** apoyado en las categorías de manejo de la UICN,
no un dictamen legal. Solo deciden los polígonos que contienen el punto del
lugar y que están vigentes (una propuesta, `Proposed`, no decide).

| Lo que dice la base en el punto | Estado | ¿Se reserva? |
|---|---|---|
| Alguna área de categoría Ia, Ib, II, III o IV | `protegido` | No. La agencia ve el área, su designación, su categoría y la autoridad que informa la base |
| Solo áreas de categoría V o VI, o sin categoría informada (`Not Reported`, `Not Applicable`, `Not Assigned`) | `permiso` | Sí, **declarando el permiso** de la autoridad del área (`permit_reference`) |
| Ninguna área | `libre` | Sí |
| El lugar aún no se pudo consultar | `sin_dato` | No |

Las categorías I a IV son la lectura habitual de «protección estricta»; V y VI,
de «uso múltiple». Las designaciones internacionales (Patrimonio Mundial,
Ramsar, Reserva de la Biosfera) no tienen categoría UICN: por sí solas piden
permiso; si coinciden con un área estricta, manda la estricta.

Las áreas estrictas que tocan el recuadro de 18 km que se analiza sin contener
el punto van en `nearby_strict`: son un aviso y no cambian el estado.

El permiso **no se verifica**: se guarda con el turno y el administrador lo ve.

Con la versión de octubre de 2026: 11 lugares `protegido`, 7 `permiso` y 19
`libre`.

### Decisiones del administrador

El administrador puede cambiar el estado de un lugar con un motivo que las
agencias siempre ven:

- `cerrado`: nadie puede reservar (por ejemplo, por una erupción). Vale para
  cualquier lugar. No cancela los turnos ya reservados.
- `habilitado`: un lugar `protegido` o `sin_dato` pasa a reservarse declarando
  el permiso. El motivo debe decir el permiso y la autoridad. Intentarlo en un
  lugar que la base no bloquea responde 409 `not_blocked`.

Hay una decisión por lugar; guardar otra la reemplaza y borrarla devuelve el
lugar a lo que dice la base.

## Reglas de un turno

- Un rango de días completos, con el primero y el último incluidos. Un lugar
  solo puede tenerlo una agencia a la vez: dos turnos chocan si comparten al
  menos un día.
- Si el lugar está libre en esas fechas, el turno queda `reservado` al instante.
- Límites (constantes en `api/services/reservations.py`, también en
  `GET /places`): como máximo 60 días por turno, con hasta 730 días (24 meses)
  de anticipación y 5 turnos activos por agencia.
- El servidor cuenta los días en UTC. Acepta un inicio de hasta un día atrás
  porque un navegador al oeste de Greenwich puede seguir en el día anterior.
- Solo reservan las agencias. El administrador gestiona; no reserva.
- Cancelar no borra: el turno queda `cancelado` con quién lo canceló y el
  motivo. La agencia cancela los suyos (motivo opcional); el administrador,
  cualquiera, y el motivo es obligatorio. Un turno que ya terminó no se cancela.
- Al eliminar la cuenta de una agencia se borran sus turnos.

**Dos peticiones a la vez.** Antes de comprobar el choque, la transacción toma
un bloqueo consultivo de PostgreSQL por lugar
(`pg_advisory_xact_lock(hashtext('lugar:<id>'))`) y otro por agencia, siempre en
el mismo orden. Así dos peticiones simultáneas para el mismo lugar se atienden
una tras otra y solo la primera reserva. Comprobado contra Neon: de 8 peticiones
simultáneas de dos agencias para el mismo lugar y fechas, una responde 201 y
siete 409.

## Objetos

Estado de un lugar:

```json
{
  "id": "sierra-negra",
  "name": "Sierra Negra",
  "province": "Galápagos",
  "country": "Ecuador",
  "status": "protegido",
  "bookable": false,
  "permit_required": false,
  "reason": "Está dentro de Galápagos (Parque Nacional, categoría UICN II): protección estricta. PROMETHEUS no asigna turnos en áreas de protección estricta (categorías UICN Ia a IV). Autoridad que la base informa: Ministry of Environment.",
  "dataset_status": "protegido",
  "areas": [
    {"site_id": 187, "name": "Galápagos", "name_eng": "Galápagos", "designation": "Parque Nacional",
     "designation_eng": "National Park", "designation_type": "National", "iucn": "II", "status": "Designated",
     "status_year": 1996, "governance": "Federal or national ministry or agency",
     "authority": "Ministry of Environment", "iso3": "ECU", "strict": true}
  ],
  "nearby_strict": [],
  "override": null,
  "occupied_today": false,
  "active_reservations": 0
}
```

- `status`: `libre`, `permiso`, `protegido`, `sin_dato`, `cerrado` o
  `habilitado`. `dataset_status` es lo que dice la base sola, sin la decisión
  del administrador.
- `areas`: las áreas vigentes que contienen el punto, con los atributos de la
  base tal cual. `name` es el nombre original, o el inglés si el original no usa
  el alfabeto latino.
- `override`: `{"mode","reason","updated_at"}` o `null`.
- `active_reservations`: turnos reservados que aún no terminan.

Turno:

```json
{
  "id": "6f1c0b9e-2f0e-4c1b-9a53-0d4b7a1f3c22",
  "code": "TUR-7K3Q9D",
  "place_id": "pampa-de-la-joya",
  "place_name": "Pampa de La Joya",
  "start_date": "2027-06-01",
  "end_date": "2027-06-07",
  "days": 7,
  "status": "reservado",
  "phase": "proximo",
  "permit_reference": null,
  "mission": "Prueba de movilidad",
  "robot": "VIPER",
  "people": 4,
  "estimated_cost_usd": 18250.0,
  "created_at": "2026-11-15T09:10:00.456Z",
  "cancelled_at": null,
  "cancelled_by": null,
  "cancel_reason": null,
  "agency": {"user_id": "…", "email": "agencia@example.org", "agency": "Agencia TEST", "acronym": "AT", "country": "Chile"}
}
```

- `status`: `reservado` o `cancelado`. `phase` añade el calendario: `proximo`,
  `en_curso`, `terminado` o `cancelado`.
- `code` es una referencia corta para citar el turno.
- `mission`, `robot`, `people` y `estimated_cost_usd` son una copia del plan de
  campaña con el que se reservó; pueden ser `null`.

## Rutas

| Método y ruta | Quién | Cuerpo | Éxito |
|---|---|---|---|
| `GET /places` | cuenta activa | – | 200 `{"source","strict_categories","limits","today","places":[lugar,…]}` |
| `GET /places/{place_id}/availability` | cuenta activa | – | 200 `{"place","busy":[…],"today","limits"}` |
| `POST /places/{place_id}/override` | admin | `{"mode":"cerrado"\|"habilitado","reason"}` | 200 lugar |
| `DELETE /places/{place_id}/override` | admin | – | 200 lugar (también si no había decisión) |
| `GET /reservations/mine` | cuenta activa | – | 200 `{"today","reservations":[turno,…]}`, del más reciente al más antiguo |
| `POST /reservations` | agencia | ver abajo | 201 turno |
| `POST /reservations/{id}/cancel` | dueña del turno o admin | `{"reason"?}` u omitido | 200 turno |
| `GET /reservations` | admin | – | 200 `{"today","reservations":[turno,…]}` por fecha de inicio |

- **`busy`** en la disponibilidad: `{"start_date","end_date","mine","agency"}`
  de cada turno reservado que aún no termina. Una agencia no ve de quién es
  (`agency` es `null`); el administrador sí.
- **`POST /reservations`**: `{"place_id","start_date","end_date"}` con fechas
  `AAAA-MM-DD`, y opcionales `permit_reference` (hasta 200 caracteres),
  `mission` (120), `robot` (80), `people` (1 a 500) y `estimated_cost_usd`.
  Un `permit_reference` enviado para un lugar que no lo pide no se guarda.
- **`GET /reservations`** admite `?status=reservado|cancelado` y `?place_id=`.
- **`source`** en `GET /places`: `name`, `url`, `terms`, `citation`, `release`
  (mes de la versión) y `fetched`. Quien muestre estos datos debe mostrar la
  cita y el mes.

## Errores

Forma común `{"detail": {"code": "...", "message": "..."}}`, con `message` en
español. `place_busy` añade `detail.busy`.

| Código | HTTP | Cuándo |
|---|---|---|
| `not_authenticated`, `session_expired` | 401 | Sin sesión válida |
| `forbidden` | 403 | Cuenta pendiente; una agencia en una ruta del administrador; el administrador intentando reservar |
| `place_not_found` | 404 | El lugar no está en el catálogo |
| `reservation_not_found` | 404 | El turno no existe o es de otra agencia |
| `place_busy` | 409 | Las fechas chocan con un turno reservado. `detail.busy` trae los rangos `{"start_date","end_date","mine"}` |
| `place_blocked` | 409 | Área de protección estricta, lugar cerrado o sin consultar. El mensaje dice el motivo |
| `too_many_reservations` | 409 | La agencia ya tiene 5 turnos activos |
| `already_cancelled` | 409 | El turno ya estaba cancelado |
| `reservation_finished` | 409 | El turno ya terminó |
| `not_blocked` | 409 | Se quiso habilitar un lugar que la base no bloquea |
| `invalid_dates` | 422 | Fin anterior al inicio, inicio pasado, más de 60 días, más de 24 meses de anticipación o fecha inexistente |
| `permit_required` | 422 | El lugar pide permiso y no se declaró |
| `reason_required` | 422 | Falta el motivo (cancelación por el administrador, decisión sobre un lugar) |
| `invalid_request` | 422 | Cuerpo mal formado, campos desconocidos o fuera de rango |
| `places_unavailable` | 503 | No se pudo leer `data/lugares.json` |
| `database_unavailable`, `auth_not_configured` | 503 | Igual que en las cuentas |

## Tablas

`reservations`: `id`, `code` (único), `user_id` (se borra con el usuario),
`place_id`, `place_name` (como se llamaba al reservar), `start_date`,
`end_date`, `status`, `permit_reference`, `mission`, `robot`, `people`,
`estimated_cost_usd`, `created_at`, `cancelled_at`, `cancelled_by`,
`cancel_reason`. Índice por `place_id, status, start_date, end_date`.

`place_overrides`: `place_id` (clave), `mode`, `reason`, `updated_at`,
`updated_by`.

## Límites conocidos

- La regla usa el punto del lugar. El área de trabajo real puede salirse de él:
  por eso se avisa cuando el recuadro de 18 km toca un área estricta.
- El permiso declarado es texto libre y nadie lo comprueba.
- No hay lista de espera ni avisos por correo: la agencia ve el motivo de una
  cancelación al abrir «Mis turnos».
- El límite de 5 turnos activos es por cuenta, no por agencia con varias cuentas.
