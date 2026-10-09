# pss-client

Cliente **no oficial** de la API de Pixel Starships con un **dashboard web** para tu cuenta real del juego: estado de la nave, tripulación con HP máximo derivado del catálogo, habitaciones, misiones e inmunidad PvP, todo en `http://localhost:8000`.

> ⚠️ **Cuenta REAL.** El login usa tu email y contraseña de Pixel Starships contra la API de producción (`api.pixelstarships.com`), y el servidor envía heartbeats que marcan tu cuenta como *Online*. Los endpoints de `/game/*` **escriben en tu cuenta** (recolectar minerales, reclamar la recompensa diaria). Proyecto no afiliado a Savy Soda / Pixel Starships.

## Arquitectura

```
frontend/index.html          dashboard (JS vanilla, guarda la sesión en localStorage)
        │  fetch JSON
        ▼
pss_client/api.py             gateway FastAPI en :8000 (sesiones + heartbeat en background)
        │  aiohttp, XML
        ▼
api.pixelstarships.com        UserService / ShipService / DesignService / TaskService / ...
```

- `pss_client/` — backend Python: `transport` (HTTP+XML), `auth` (login email+password), `ship` (nave), `game` (acciones), `designs` (catálogo), `api` (gateway REST).
- `frontend/index.html` — dashboard de una sola página: Resumen, Nave, Tripulación, Habitaciones, Misiones.
- Las respuestas de la API de PSS son XML (con errores como atributo `errorMessage` y HTTP 200 incluso en fallos — ver `pss_client/transport.py`).

## Setup

Requiere Python ≥ 3.11 y [uv](https://docs.astral.sh/uv/):

```bash
uv sync                          # dependencias (fastapi, uvicorn, aiohttp, pydantic)
uv run python -m pss_client.api  # gateway en http://localhost:8000
```

Abre `http://localhost:8000` en el navegador, entra con tu email y contraseña del juego y verás el dashboard.

Smoke test del paso 1 del login contra la API real (sin credenciales propias):

```bash
uv run python test_auth.py
```

Log del servidor: `pss-client.log` en la raíz del repo (nivel DEBUG: cada request/response contra la API de PSS).

### CLI

Además del gateway hay una CLI con subcomandos que usa la misma sesión persistida (`~/.pss-client/tokens.json`) y el mismo cliente HTTP — el flujo de login sigue siendo el del dashboard web:

```bash
uv run pss-client whoami           # o: uv run python -m pss_client.cli whoami
```

`whoami` verifica la sesión en vivo contra `UserService/GetCurrentUser` (el mismo endpoint que `POST /user/me`) y muestra el email y el estado de la sesión. Es de **solo lectura**: nunca renueva tokens ni escribe `tokens.json`. Con varias cuentas guardadas, `--email` elige una (por defecto: la más reciente); `--json` serializa la salida para scripts; `--debug` muestra cada request.

Códigos de salida: `0` sesión activa · `2` sin sesión guardada (con pista de cómo loguearse) · `3` sesión expirada (token rechazado por la API) · `1` error de red/API inesperado. Ningún path imprime un stack trace crudo.


### Variables de entorno (opcionales)

| Variable | Default | Para qué |
|---|---|---|
| `PSS_CHECKSUM_KEY` | `5343` | Clave del checksum (extraída del cliente; configurable para no commitearla) |
| `PSS_SAVY_CHECKSUM` | `Savvy!s0d@` | Sufijo del checksum |

## Login y sesión

El login replica el flujo de pss-companion.com, reconstruido del desensamblado del cliente oficial (detalle completo y fórmulas en [docs/REVERSE-ENGINEERING.md](docs/REVERSE-ENGINEERING.md)):

1. `DeviceLogin17` con un deviceKey determinista derivado del email → cuenta web temporal + accessToken.
2. `UserEmailPasswordAuthorize4` con ese accessToken → **refreshToken + UserId de tu cuenta real**.
3. `DeviceLogin17` con el refreshToken → accessToken owner.

El `refreshToken` se persiste en `~/.pss-client/tokens.json` (junto al accessToken y su `saved_at`), así que:

- La sesión **sobrevive reinicios del servidor**: al arrancar se restauran las sesiones y el navegador entra sin re-login. Si tu sesión es previa al fix del `session_token` estable (commit `d9aebeb`, issue #5), se pedirá re-login **una sola vez**.
- El **accessToken caduca a ~52 min** (medido, issue #3); el gateway lo **renueva proactivamente a los 45 min** con el refreshToken — y de forma reactiva si una petición lo encuentra muerto.
- Mientras haya una sesión activa, un loop envía `HeartBeat4` cada 60 s: mantiene la cuenta **Online** igual que el cliente del juego.

El `session_token` que usa el navegador es estable por cuenta (`uuid5` del email), no rota con las renovaciones del accessToken (issue #5).

## Endpoints del gateway

Todos los endpoints (salvo `/` y `/health`) son `POST` con JSON y se autentican con `session_token`. Referencia completa con ejemplos en [docs/API.md](docs/API.md).

| Endpoint | Qué hace |
|---|---|
| `POST /auth/login` | Login email+password → `session_token` |
| `POST /auth/logout` | Cierra la sesión local |
| `POST /auth/refresh` | Fuerza la renovación del accessToken (tests) |
| `POST /user/me` | Datos de la cuenta (nombre, créditos, trofeos…) |
| `POST /ship/info` | Nave + tripulación + habitaciones + items, con campos derivados del catálogo (max HP, nombres, tipo/nivel de sala, inmunidad PvP, salas de escudo) |
| `POST /game/tasks` | Misiones diarias |
| `POST /game/researches` | Investigaciones en curso |
| `POST /game/star-systems` | Sistemas estelares visitados |
| `POST /game/designs` | Catálogo de diseños (ids → nombres) |
| `POST /game/collect-resources` | ⚠️ Acción: recolecta minerales de una habitación |
| `POST /game/collect-daily-reward` | ⚠️ Acción: reclama la recompensa diaria |
| `POST /game/heartbeat` | Envía un heartbeat manual (cuenta Online) |

## Documentación

- [docs/REVERSE-ENGINEERING.md](docs/REVERSE-ENGINEERING.md) — el activo más valioso del repo: checksums desensamblados y verificados, flujo de login, fórmulas de stats del juego (`GetStatForLevel`, `get_MaxShield`), hallazgos de mecánicas y cómo reproducir la extracción desde el binario IL2CPP.
- [docs/API.md](docs/API.md) — referencia de los endpoints del gateway con ejemplos de request/response.

## Estado y limitaciones

- Solo lectura en su mayor parte; las acciones implementadas son recolectar recursos y la recompensa diaria (issue #6).
- El escudo de la nave **solo existe en batalla**: la API siempre reporta `Shield=0` fuera de combate; el dashboard muestra la inmunidad PvP (`ImmunityDate`) como protección real (issues #1 y #4).
- El max HP de personajes se deriva del catálogo **sin** el bonus de entrenamiento del gym — el XML sí expone `HpImprovement` y quedará aplicarlo (issue #7, abierto).
- `Hp` del casco es el daño real pendiente de reparar (no se regenera solo).
- Casos abiertos de robustez de sesiones en el issue #9 (circuit-breaker de refresh fallido, vida del refreshToken).

## Licencia

MIT. Proyecto no oficial, sin relación con Pixel Starships ni Savy Soda.
