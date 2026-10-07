# Referencia de la API del gateway

Gateway FastAPI servido en `http://localhost:8000` (`uv run python -m pss_client.api`). Sirve el dashboard en `/` y expone los endpoints REST de abajo.

Convenciones:

- Todos los endpoints (salvo `/` y `/health`) son **POST con body JSON** y se autentican con `session_token` (obtenido en `/auth/login`, guardado por el frontend en `localStorage`).
- `session_token` es **estable por cuenta** (`uuid5` del email): no rota con las renovaciones del accessToken ni con reinicios del server (issue #5).
- Sesión inválida → **401** con body `{"detail": {"reason": "session_expired", "message": "Sesión expirada, inicia sesión de nuevo"}}`.
- Errores de la API de PSS → **400/500** con `detail` descriptivo.

## Endpoints públicos

### `GET /`
Sirve `frontend/index.html` (el dashboard).

### `GET /health`
`{"status": "ok", "service": "pss-api-gateway"}`

## Autenticación

### `POST /auth/login`

Login con email y contraseña de Pixel Starships (flujo companion de 3 pasos — ver [REVERSE-ENGINEERING.md](REVERSE-ENGINEERING.md) §3). Con éxito arranca el loop de heartbeat (60 s) para esa sesión y persiste el refreshToken en `~/.pss-client/tokens.json`.

Request:

```json
{ "email": "tu@email.com", "password": "tu-contraseña" }
```

Response 200:

```json
{
  "success": true,
  "session_token": "sess_11586440_1f0e3d99-…",
  "user_id": 11586440,
  "access_token": "62c59a76-…",
  "user": {
    "user_id": 11586440,
    "name": "NombreDeUsuario",
    "email": "tu@email.com",
    "user_type": "NormalUser",
    "credits": 1234,
    "trophy": 56
  }
}
```

Errores **401**:

- `Email o contraseña incorrectos. Verifica tus credenciales de Pixel Starships.` — credenciales erróneas.
- `El servidor rechazó el login. Verifica email y contraseña. (Si tu cuenta es solo-Steam, este método no aplica).` — checksum rechazado o cuenta solo-Steam.

### `POST /auth/logout`

Cierra la sesión local (borra el token del dict en memoria y detiene su heartbeat). El refreshToken persistido NO se borra: el próximo `/auth/login` (o un reinicio del server) reentra directo con el paso 0.

```json
{ "session_token": "sess_…" }
```

Response: `{"success": true}`

### `POST /auth/refresh`

Fuerza la renovación del accessToken de la sesión con su refreshToken persistido — útil para tests E2E sin esperar los 45 min de la renovación proactiva (commit `f4ce0fd`, issue #3).

```json
{ "session_token": "sess_…" }
```

Response: `{"success": true, "renewed": true}` — `renewed=false` si la sesión no existe o el refreshToken ya no sirve (tocará re-loguear).

## Cuenta y nave

### `POST /user/me`

Datos de la cuenta autenticada (`UserService/GetCurrentUser`). Renueva el accessToken si lo encuentra caducado (issue #5).

```json
{ "session_token": "sess_…" }
```

```json
{
  "success": true,
  "user": {
    "user_id": "11586440",
    "name": "NombreDeUsuario",
    "email": "tu@email.com",
    "user_type": "NormalUser",
    "credits": "1234",
    "trophy": "56",
    "gender": "1",
    "race": "2",
    "last_login": "2026-10-06T…"
  }
}
```

(los valores llegan como strings — atributos crudos del XML de PSS)

### `POST /ship/info`

La nave del usuario (`ShipService/GetShipByUserId`) **enriquecida con joins del catálogo** (`DesignService`). Renueva el accessToken si lo encuentra caducado.

```json
{ "session_token": "sess_…" }
```

Campos derivados y qué significan:

- `ship.hp` — casco: es el **daño pendiente de reparar**, no un HP actual (issue #1).
- `ship.shield` — **siempre 0 fuera de batalla** (issue #1); ver `shield_info`.
- `immunity` — inmunidad PvP derivada de `ImmunityDate` (UTC): `{active: bool, until: "2026-10-04T19:06:36", raw: …}` (issue #4).
- `shield_info.rooms` — las salas de escudo del usuario con su `capacity` del catálogo (el `MaxShield` del juego es `Σ capacity × 100` — issue #8).
- `characters[].max_hp` — HP máximo derivado: interpolación `GetStatForLevel` sobre los stats del `CharacterDesign` (issue #2). **Sin bonus de gym** por ahora (issue #7, abierto).
- `rooms[].name/type/level/category` — resueltos del catálogo (p. ej. `"Med Bay Lv6"`, `"Reactor"`, `"Computer"`).

Response (recortado):

```json
{
  "success": true,
  "shield_info": {
    "note": "El escudo de la nave solo existe EN BATALLA… El equivalente fuera de batalla es la INMUNIDAD PvP (ImmunityDate)…",
    "rooms": [
      { "room_id": 12345, "room_design_id": 2000,
        "name": "Shield Generator Lv15", "status": "Activate",
        "max_system_power": "3", "capacity": "12" }
    ]
  },
  "immunity": { "active": false, "until": "2026-10-04T19:06:36", "raw": "2026-10-04T19:06:36" },
  "ship": {
    "ship_id": 8761013, "ship_name": "…", "ship_level": 14,
    "ship_design_id": 386, "ship_status": "Defending",
    "hp": 39, "shield": 0, "power_score": 2446,
    "star_system_id": 8, "immunity_date": "2026-10-04T19:06:36",
    "status_start_date": "…", "update_date": "…",
    "characters_count": 17, "rooms_count": 25, "items_count": 40
  },
  "characters": [
    { "character_id": 33001, "character_design_id": 30,
      "name": "Ninja rojo", "level": 33,
      "hp": 0, "max_hp": 6.3, "shield": 0,
      "race_id": 1, "weapon_design_id": 1002, "ability_design_id": 0,
      "room_id": 14001 }
  ],
  "rooms": [
    { "room_id": 14001, "room_design_id": 2001, "room_status": "Activate",
      "assigned_power": 2, "system_power": 2, "current_capacity": 0,
      "character_ids": [33001],
      "name": "Med Bay Lv6", "room_type": "Operate", "level": "6", "category": "1" }
  ],
  "items": [
    { "item_id": 99001, "item_design_id": 55, "amount": 3, "room_id": 14001 }
  ]
}
```

Notas: `characters[].hp` es el `BattleCharacterHp` del XML (fuera de batalla suele ser 0). El ejemplo usa valores ilustrativos de la flota real documentada en los issues (nave 8761013, Shield Generator Lv15 capacity 12).

## Juego — consultas

### `POST /game/tasks`

Misiones diarias (`TaskService/ListTasksOfAUser`):

```json
{ "success": true,
  "tasks": [ { "task_id": 123, "task_design_id": 45, "progress_value": 2.0, "collected": false } ] }
```

`task_design_id` se resuelve a nombre/descripción con `/game/designs` (`task_designs`).

### `POST /game/researches`

Investigaciones (`ResearchService/ListAllResearches`):

```json
{ "success": true, "researches": [ { "research_id": 77, "design_id": 12 } ] }
```

### `POST /game/star-systems`

Sistemas estelares visitados (`GalaxyService/ListUserStarSystems`):

```json
{ "success": true, "star_systems": [ { "star_system_id": 8 } ] }
```

### `POST /game/designs`

Catálogo de diseños cacheado en memoria del proceso: `{colección: {"id": nombre}}`. Colecciones: `room_designs`, `character_designs`, `character_stats`, `room_types`, `room_stats`, `ship_designs`, `research_designs`, `task_designs`, `item_designs` (issues #2, catálogo descargado con el flujo de 3 pasos del Setting — ver [REVERSE-ENGINEERING.md](REVERSE-ENGINEERING.md) §7).

```json
{ "session_token": "sess_…" }
```

```json
{ "success": true,
  "designs": {
    "character_designs": { "30": "Ninja rojo", "…": "…" },
    "room_designs":      { "2001": "Med Bay Lv6", "…": "…" },
    "task_designs":      { "45": "Win 3 PvP battles", "…": "…" }
  }
}
```

## Juego — acciones ⚠️

Estos endpoints **escriben en tu cuenta real**.

### `POST /game/collect-resources`

Recolecta minerales de una habitación (`RoomService/CollectResources3` con checksum genérico):

```json
{ "session_token": "sess_…", "room_id": 14001, "amount": 100 }
```

Response: `{"success": true, "raw": "<RoomService>…XML…</RoomService>"}` · Error: **400** con el `errorMessage` de la API.

### `POST /game/collect-daily-reward`

Reclama la recompensa diaria (`UserService/CollectDailyReward2`):

```json
{ "session_token": "sess_…" }
```

Response: `{"success": true, "raw": "<UserService>…"}`

### `POST /game/heartbeat`

Envía un heartbeat manual — marca la cuenta Online ahora mismo (`UserService/HeartBeat4` con el checksum INT desensamblado). No suele hacer falta: el loop del server lo envía cada 60 s por sesión activa.

```json
{ "session_token": "sess_…" }
```

Response: `{"success": true, "raw": "…"}` — con token inactivo demasiado tiempo la API responde `{"success": false, "message": "You have been idle for too long."}` (errorCode=1).

## Sesiones del servidor (contexto)

- Al arrancar, el gateway **restaura** las sesiones con accessToken persistido en `~/.pss-client/tokens.json` y reactiva sus heartbeats — el navegador entra sin re-login tras un reinicio (issue #5).
- El accessToken caduca a ~52 min; se renueva **proactivamente a los 45 min** de emisión real y de forma reactiva en cualquier endpoint si falla la autorización (issues #3, #5; commit `f4ce0fd`).
