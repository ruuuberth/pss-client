"""API REST wrapper para pss_client - permite usar el backend desde un frontend web.

Método de autenticación ÚNICO: email + contraseña (flujo pss-companion de 3 pasos).
Los demás métodos (device-key, steam, checksum debug) fueron eliminados a petición.
"""

import logging
import sys

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import asyncio
import os
from datetime import datetime, timezone

from pss_client.auth import PssAuth, UserLogin
from pss_client.transport import PssApiError, PssHttpClient
from pss_client.ship import PssShipService
from pss_client.game import PssGameService, create_action_checksum
from pss_client.designs import get_design_catalog, stat_for_level

# --- Logging: archivo + consola ---------------------------------------------- #
LOG_FILE = os.path.join(os.path.dirname(__file__), "..", "pss-client.log")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("pss_client.api")

app = FastAPI(title="PSS API Gateway", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir archivos estáticos del frontend
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

@app.get("/")
async def root():
    index_path = os.path.join(frontend_dir, "index.html")
    return FileResponse(index_path)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "pss-api-gateway"}

# Estado global de sesiones autenticadas (en producción usar Redis/DB)
active_sessions: dict[str, UserLogin] = {}
_heartbeat_tasks: dict[str, asyncio.Task] = {}  # session_token -> task


def _immunity_info(immunity_date: Optional[str]) -> dict:
    """Interpreta ImmunityDate: el 'escudo de protección' PvP del juego.

    El juego lo llama "immunity shields" ("You can use immunity shields to
    prevent your ship from being attacked while you are away"). Las fechas
    de la API vienen en UTC (verificado: UpdateDate coincide con la hora de
    las peticiones).
    """
    from datetime import datetime, timezone
    if not immunity_date:
        return {"active": False, "until": None, "raw": None}
    active = False
    try:
        dt = datetime.strptime(immunity_date[:19], "%Y-%m-%dT%H:%M:%S")
        active = dt.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    except ValueError:
        pass
    return {"active": active, "until": immunity_date, "raw": immunity_date}


def _restore_sessions() -> int:
    """Restaura las sesiones persistidas en ~/.pss-client/tokens.json.

    Al arrancar, las cuentas con accessToken guardado vuelven a estar
    disponibles sin re-login (el navegador conserva su session_token).
    """
    from pss_client.auth import _load_tokens
    restored = 0
    for email, t in _load_tokens().items():
        at = t.get("access_token") or ""
        uid = int(t.get("user_id") or 0)
        if at and uid:
            from pss_client.auth import _session_token_for
            session_token = _session_token_for(email, uid)
            active_sessions[session_token] = UserLogin(
                access_token=at, user_id=uid,
                previous_last_login_date=None, raw={})
            restored += 1
            logger.info("[startup] sesión restaurada user=%s (%s…)", uid, session_token[:24])
    if restored:
        logger.info("[startup] %d sesión(es) restaurada(s) al arrancar", restored)
    return restored


@app.on_event("startup")
async def _on_startup() -> None:
    n = _restore_sessions()
    for token in list(active_sessions):
        _start_heartbeat(token)
    if n:
        logger.info("[startup] heartbeat reactivado para %d sesión(es)", n)


async def _refresh_session_token(session_token: str) -> bool:
    """Renueva el accessToken caducado de una sesión con su refreshToken.

    Los accessToken de PSS duran ~1h (verificado 2026-10-04: la sesión
    funcionó 51 min y luego "Failed to authorize access token."). El juego
    pide uno nuevo con el RefreshToken persistido — mismo canje del paso 3.
    El session_token del navegador NO cambia (se actualiza el token interno).
    """
    login = active_sessions.get(session_token)
    if not login:
        return False
    from pss_client.auth import PssAuth, _email_for_user, _load_tokens, _save_token
    email = _email_for_user(login.user_id)
    if not email:
        logger.warning("[refresh] sin refreshToken persistido para user %s", login.user_id)
        return False
    entry = _load_tokens().get(email) or {}
    refresh = entry.get("refresh_token") or ""
    if not refresh:
        return False
    auth = PssAuth()
    try:
        new_login = await auth.refresh_access_token(
            entry.get("device_key") or "", refresh)
        if new_login and new_login.user_id == login.user_id:
            login.access_token = new_login.access_token
            _save_token(email, entry.get("device_key") or "", refresh,
                        new_login.user_id, access_token=new_login.access_token)
            logger.info("[refresh] accessToken renovado para user %s", login.user_id)
            return True
        logger.warning("[refresh] el refreshToken no devolvió token para user %s", login.user_id)
    except Exception as exc:
        logger.warning("[refresh] fallo renovando token de user %s: %r", login.user_id, exc)
    finally:
        await auth.close()
    return False


# Renovación PROACTIVA: los accessToken de PSS viven ~52 min (medida real
# del log 2026-10-04: 51 heartbeats OK consecutivos → "Failed to authorize
# access token", ver issue #3). Se renueva a los 45 min con margen, ANTES
# del fallo reactivo del heartbeat.
PROACTIVE_REFRESH_AFTER = 45 * 60  # segundos


def _access_token_age_seconds(login: UserLogin) -> Optional[float]:
    """Edad del accessToken actual según el 'saved_at' persistido en tokens.json.

    saved_at se actualiza en cada renovación (_save_token lo escribe siempre),
    así que refleja la emisión REAL del token — incluso tras un reinicio del
    server (_restore_sessions trae tokens ya viejos; usar la hora de arranque
    del loop los trataría como recién emitidos y dejaría pasar la expiración).
    Devuelve None si no hay saved_at (se renueva por la vía reactiva).
    """
    from pss_client.auth import _email_for_user, _load_tokens
    email = _email_for_user(login.user_id)
    if not email:
        return None
    entry = _load_tokens().get(email) or {}
    saved_at = entry.get("saved_at") or ""
    try:
        emitted = datetime.strptime(saved_at[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - emitted).total_seconds()


async def _heartbeat_loop(session_token: str, interval: int = 60):
    """Envía HeartBeat4 cada `interval` segundos mientras la sesión viva.

    Replica el heartbeat del juego (UserManager.CheckHeartbeat -> POST
    UserService/HeartBeat4) para mantener la cuenta marcada Online en el
    servidor de PSS (ShipStatus Online en el XML de la nave).

    Además del reintento reactivo (fallo → renovar → reintentar), renueva
    PROACTIVAMENTE el accessToken a los 45 min de emisión para que el
    heartbeat (y cualquier llamada del usuario) nunca vea un token muerto.
    """
    login = active_sessions.get(session_token)
    if not login:
        return
    logger.info("[heartbeat] iniciado para user %s (cada %ss)", login.user_id, interval)
    try:
        while True:
            await asyncio.sleep(interval)
            login = active_sessions.get(session_token)
            if not login:
                break  # sesión cerrada
            # Renovación PROACTIVA: a los 45 min de EMISIÓN (saved_at real,
            # no de arranque del loop) pedir token nuevo antes del fallo.
            age = _access_token_age_seconds(login)
            if age is not None and age >= PROACTIVE_REFRESH_AFTER:
                if await _refresh_session_token(session_token):
                    logger.info(
                        "[heartbeat] token renovado proactivamente para user %s "
                        "(age %.0f min)", login.user_id, age / 60)
                    continue  # token fresco → siguiente ciclo normal
                logger.warning(
                    "[heartbeat] renovación proactiva falló para user %s "
                    "(age %.0f min) — la reactiva actuará al fallar",
                    login.user_id, age / 60)
            http = PssHttpClient()
            try:
                game = PssGameService(http)
                await game.send_heartbeat(login.access_token)
            except Exception as exc:
                # token caducado (~1h) → renovar con el refresh y reintentar
                logger.warning("[heartbeat] fallo para user %s: %r — renovando token",
                               login.user_id, exc)
                if await _refresh_session_token(session_token):
                    try:
                        game = PssGameService(http)
                        await game.send_heartbeat(login.access_token)
                        logger.info("[heartbeat] OK tras renovar el token")
                    except Exception as exc2:
                        logger.warning("[heartbeat] sigue fallando: %r", exc2)
            finally:
                await http.close()
    finally:
        _heartbeat_tasks.pop(session_token, None)
        logger.info("[heartbeat] detenido para session %s…", session_token[:20])


def _start_heartbeat(session_token: str):
    if session_token not in _heartbeat_tasks:
        _heartbeat_tasks[session_token] = asyncio.create_task(
            _heartbeat_loop(session_token))


# --- Auth: ÚNICO método email+password -------------------------------------- #

class LoginRequest(BaseModel):
    email: str
    password: str


class SessionRequest(BaseModel):
    session_token: str


@app.post("/auth/login")
async def login(req: LoginRequest):
    """Login con email y contraseña (flujo pss-companion de 3 pasos)."""
    logger.info("[login] intento login email=%s", req.email)
    async with PssAuth() as auth:
        try:
            login = await auth.email_password_login(
                email=req.email,
                password=req.password,
            )
            from pss_client.auth import _session_token_for
            session_token = _session_token_for(req.email, login.user_id)
            active_sessions[session_token] = login
            _start_heartbeat(session_token)
            logger.info("[login] LOGIN OK user_id=%s session=%s (heartbeat ON)",
                        login.user_id, session_token)
            # Datos del usuario para el dashboard (nombre, créditos, trofeos...)
            user_info = _extract_user_info(login)
            return {
                "success": True,
                "session_token": session_token,
                "user_id": login.user_id,
                "access_token": login.access_token,
                "user": user_info,
            }
        except PssApiError as e:
            msg = str(e)
            logger.warning("[login] FALLO email=%s error=%r", req.email, msg)
            if "Email or password is incorrect" in msg or "Email o contraseña incorrectos" in msg:
                msg = "Email o contraseña incorrectos. Verifica tus credenciales de Pixel Starships."
            elif "An error occurred" in msg:
                msg = ("El servidor rechazó el login. Verifica email y contraseña. "
                       "(Si tu cuenta es solo-Steam, este método no aplica).")
            raise HTTPException(status_code=401, detail=msg)


def _extract_user_info(login: UserLogin) -> dict:
    """Extrae los datos del <User> embebido en raw (si existe)."""
    raw = getattr(login, "raw", None) or {}
    info = {
        "user_id": login.user_id,
        "name": raw.get("Name"),
        "email": raw.get("Email"),
        "user_type": raw.get("UserType"),
        "credits": raw.get("Credits"),
        "trophy": raw.get("Trophy"),
    }
    return {k: v for k, v in info.items() if v is not None}


# --- Sesión ----------------------------------------------------------------- #

def _require_session(session_token: str) -> UserLogin:
    login = active_sessions.get(session_token)
    if not login:
        # 401 estructurado: el frontend detecta reason=session_expired y
        # muestra el aviso en el login en vez de limpiar en silencio.
        raise HTTPException(status_code=401, detail={
            "reason": "session_expired",
            "message": "Sesión expirada, inicia sesión de nuevo",
        })
    return login


@app.post("/auth/logout")
async def logout(req: SessionRequest):
    """Cierra la sesión local (borra el token del dict en memoria)."""
    if req.session_token in active_sessions:
        del active_sessions[req.session_token]
        logger.info("[logout] sesión cerrada %s", req.session_token[:20])
    return {"success": True}


@app.post("/auth/refresh")
async def auth_refresh(req: SessionRequest):
    """Fuerza la renovación del accessToken de una sesión (tests manuales).

    Devuelve renewed=False si la sesión no existe o el refreshToken ya no
    sirve — el caller entonces debe re-loguear (401 del resto de endpoints).
    """
    renewed = await _refresh_session_token(req.session_token)
    return {"success": True, "renewed": renewed}


# --- User / cuenta ----------------------------------------------------------- #

@app.post("/user/me")
async def user_me(req: SessionRequest):
    """Datos de la cuenta autenticada (desde GetCurrentUser)."""
    login = _require_session(req.session_token)
    http = PssHttpClient()
    try:
        try:
            resp = await http.request(
                "UserService/GetCurrentUser",
                method="GET",
                response_root_tag="GetCurrentUser",
                accessToken=login.access_token,
            )
        except PssApiError as exc:
            # accessToken caducado (~1h) → renovar y reintentar
            if "authorize access token" not in str(exc).lower():
                raise
            if not await _refresh_session_token(req.session_token):
                raise
            resp = await http.request(
                "UserService/GetCurrentUser",
                method="GET",
                response_root_tag="GetCurrentUser",
                accessToken=login.access_token,
            )
        node = resp.root if resp.root.tag == "GetCurrentUser" else resp.root.find(".//GetCurrentUser")
        if node is None:
            return {"success": True, "user": _extract_user_info(login)}
        user_node = node.find(".//User")
        user_attrs = dict(user_node.attrib) if user_node is not None else dict(node.attrib)
        return {
            "success": True,
            "user": {
                "user_id": user_attrs.get("Id") or user_attrs.get("UserId") or login.user_id,
                "name": user_attrs.get("Name"),
                "email": user_attrs.get("Email"),
                "user_type": user_attrs.get("UserType"),
                "credits": user_attrs.get("Credits"),
                "trophy": user_attrs.get("Trophy"),
                "gender": user_attrs.get("GenderType"),
                "race": user_attrs.get("RaceType"),
                "last_login": user_attrs.get("LastLoginDate"),
            },
        }
    except PssApiError as e:
        logger.warning("[user/me] error: %r", str(e))
        return {"success": True, "user": _extract_user_info(login)}
    finally:
        await http.close()


# --- Ship -------------------------------------------------------------------- #

@app.post("/ship/info")
async def get_ship_info(req: SessionRequest):
    """Obtiene la información de la nave del usuario autenticado."""
    login = _require_session(req.session_token)
    try:
        http = PssHttpClient()
        ship_service = PssShipService(http)
        try:
            ship = await ship_service.get_ship_by_user_id(
                access_token=login.access_token,
                user_id=login.user_id,
            )
        except PssApiError as exc:
            # el accessToken caducó (~1h) → renovar con el refresh y reintentar
            if "authorize access token" not in str(exc).lower():
                raise
            if not await _refresh_session_token(req.session_token):
                raise
            ship = await ship_service.get_ship_by_user_id(
                access_token=login.access_token,
                user_id=login.user_id,
            )
        await http.close()

        # Join con el catálogo: identificar las shield rooms del usuario.
        # El escudo no es un valor persistente del servidor (Shield=0 en el
        # XML siempre); lo computa el motor de batalla desde estas rooms.
        # (diagnóstico 2026-10-03, ver issue #1)
        shield_rooms = []
        char_max_hp: dict[int, float] = {}
        try:
            dhttp = PssHttpClient()
            catalog = await get_design_catalog(dhttp, login.access_token)
            room_names = catalog.get("room_designs", {})
            room_types = catalog.get("room_types", {})
            room_stats = catalog.get("room_stats", {})
            # max HP por personaje: interpolación del catálogo
            # (PSCharacter.GetStatForLevel desensamblado, ver designs.stat_for_level)
            char_stats = catalog.get("character_stats", {})
            for c in ship.characters:
                cs = char_stats.get(c.character_design_id)
                if not cs:
                    continue
                char_max_hp[c.character_id] = round(stat_for_level(
                    level=c.level,
                    basic=float(cs.get("hp", 0)),
                    final=float(cs.get("final_hp", 0)),
                    max_level=int(cs.get("max_level", 40) or 40),
                    progression=cs.get("progression", "Linear"),
                ), 1)
            for r in ship.rooms:
                if room_types.get(r.room_design_id) == "Shield":
                    stats = room_stats.get(r.room_design_id, {})
                    shield_rooms.append({
                        "room_id": r.room_id,
                        "room_design_id": r.room_design_id,
                        "name": room_names.get(r.room_design_id,
                                               f"RoomDesign {r.room_design_id}"),
                        "status": r.room_status,
                        "max_system_power": stats.get("max_system_power", ""),
                        "capacity": stats.get("capacity", ""),
                    })
            # info extendida de habitaciones: nombre, tipo y nivel del catálogo
            rooms_info = {}
            for r in ship.rooms:
                stats = room_stats.get(r.room_design_id, {})
                rooms_info[r.room_id] = {
                    "name": room_names.get(r.room_design_id,
                                          f"RoomDesign {r.room_design_id}"),
                    "type": stats.get("type", room_types.get(r.room_design_id, "")),
                    "level": stats.get("level", ""),
                    "category": stats.get("category", ""),
                }
            await dhttp.close()
        except Exception as exc:
            logger.warning("[ship/info] join de shield rooms falló: %r", exc)
        rooms_info = locals().get("rooms_info", {})
        return {
            "success": True,
            "shield_info": {
                "note": ("El escudo de la nave solo existe EN BATALLA: en el juego "
                         "el panel de escudo vive en BattleSceneUIManager (HUD de "
                         "combate) y lo generan las salas de escudo. Fuera de "
                         "combate la API reporta Shield=0 (verificado tambien en "
                         "InspectShipViaShipId). El equivalente fuera de batalla es "
                         "la INMUNIDAD PvP (ImmunityDate): el 'escudo de "
                         "proteccion' del juego."),
                "rooms": shield_rooms,
            },
            "immunity": _immunity_info(ship.immunity_date),
            "ship": {
                "ship_id": ship.ship_id,
                "ship_name": ship.ship_name,
                "ship_level": ship.ship_level,
                "ship_design_id": ship.ship_design_id,
                "ship_status": ship.ship_status,
                "hp": ship.hp,
                "shield": ship.shield,
                "power_score": ship.power_score,
                "star_system_id": ship.star_system_id,
                "next_star_system_id": ship.next_star_system_id,
                "center_x": ship.center_x,
                "center_y": ship.center_y,
                "skin_item_design_id": ship.skin_item_design_id,
                "skin_opacity_value": ship.skin_opacity_value,
                "sticker_string": ship.sticker_string,
                "tags": ship.tags,
                "immunity_date": ship.immunity_date,
                "status_start_date": ship.status_start_date,
                "update_date": ship.update_date,
                "characters_count": len(ship.characters),
                "rooms_count": len(ship.rooms),
                "items_count": len(ship.items),
            },
            "characters": [
                {
                    "character_id": c.character_id,
                    "character_design_id": c.character_design_id,
                    "name": c.name,
                    "level": c.level,
                    "hp": c.hp,
                    "max_hp": char_max_hp.get(c.character_id, c.max_hp) or c.max_hp,
                    "shield": c.shield,
                    "race_id": c.race_id,
                    "weapon_design_id": c.weapon_design_id,
                    "ability_design_id": c.ability_design_id,
                    "room_id": c.room_id,
                }
                for c in ship.characters
            ],
            "rooms": [
                {
                    "room_id": r.room_id,
                    "room_design_id": r.room_design_id,
                    "room_status": r.room_status,
                    "assigned_power": r.assigned_power,
                    "system_power": r.system_power,
                    "current_capacity": r.current_capacity,
                    "character_ids": r.characters,
                    "name": (rooms_info.get(r.room_id, {}) or {}).get("name", ""),
                    "room_type": (rooms_info.get(r.room_id, {}) or {}).get("type", ""),
                    "level": (rooms_info.get(r.room_id, {}) or {}).get("level", ""),
                    "category": (rooms_info.get(r.room_id, {}) or {}).get("category", ""),
                }
                for r in ship.rooms
            ],
            "items": [
                {
                    "item_id": i.item_id,
                    "item_design_id": i.item_design_id,
                    "amount": i.amount,
                    "room_id": i.room_id,
                }
                for i in ship.items
            ],
        }
    except PssApiError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.exception("[ship/info] error interno")
        raise HTTPException(status_code=500, detail=f"Error interno: {e}")


# --- Game: consultas y acciones para JUGAR ---------------------------------- #

@app.post("/game/tasks")
async def game_tasks(req: SessionRequest):
    """Misiones/tareas diarias del usuario."""
    login = _require_session(req.session_token)
    http = PssHttpClient()
    try:
        game = PssGameService(http)
        tasks = await game.list_tasks(login.access_token)
        return {"success": True, "tasks": [
            {"task_id": t.task_id, "task_design_id": t.task_design_id,
             "progress_value": t.progress_value, "collected": t.collected}
            for t in tasks
        ]}
    except PssApiError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await http.close()


@app.post("/game/researches")
async def game_researches(req: SessionRequest):
    """Investigaciones en curso del usuario."""
    login = _require_session(req.session_token)
    http = PssHttpClient()
    try:
        game = PssGameService(http)
        researches = await game.list_researches(login.access_token)
        return {"success": True, "researches": [
            {"research_id": r.research_id, "design_id": r.research_design_id}
            for r in researches
        ]}
    except PssApiError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await http.close()


@app.post("/game/star-systems")
async def game_star_systems(req: SessionRequest):
    """Sistemas estelares visitados por el usuario."""
    login = _require_session(req.session_token)
    http = PssHttpClient()
    try:
        game = PssGameService(http)
        systems = await game.list_star_systems(login.access_token)
        return {"success": True, "star_systems": [
            {"star_system_id": s.star_system_id} for s in systems
        ]}
    except PssApiError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await http.close()


class CollectResourcesRequest(BaseModel):
    session_token: str
    room_id: int
    amount: int


@app.post("/game/collect-resources")
async def game_collect_resources(req: CollectResourcesRequest):
    """Recolecta minerales de una habitación de la nave."""
    login = _require_session(req.session_token)
    http = PssHttpClient()
    try:
        game = PssGameService(http)
        result = await game.collect_room_resources(
            access_token=login.access_token,
            room_id=req.room_id,
            amount=req.amount,
        )
        return result
    except PssApiError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await http.close()


@app.post("/game/collect-daily-reward")
async def game_collect_daily_reward(req: SessionRequest):
    """Reclama la recompensa diaria."""
    login = _require_session(req.session_token)
    http = PssHttpClient()
    try:
        game = PssGameService(http)
        result = await game.collect_daily_reward(login.access_token)
        return result
    except PssApiError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await http.close()


@app.post("/game/heartbeat")
async def game_heartbeat(req: SessionRequest):
    """Envía un heartbeat manual (marca la cuenta Online ahora mismo)."""
    login = _require_session(req.session_token)
    http = PssHttpClient()
    try:
        game = PssGameService(http)
        result = await game.send_heartbeat(login.access_token)
        return result
    except PssApiError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await http.close()


@app.post("/game/designs")
async def game_designs(req: SessionRequest):
    """Catálogo de diseños: {coleccion: {id: nombre}} (cacheado en el proceso)."""
    login = _require_session(req.session_token)
    http = PssHttpClient()
    try:
        catalog = await get_design_catalog(http, login.access_token)
        # serializar a {coleccion: {"id": nombre}} (JSON necesita keys str)
        return {"success": True, "designs": {
            coll: {str(k): v for k, v in mapping.items()}
            for coll, mapping in catalog.items()
        }}
    except PssApiError as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await http.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
