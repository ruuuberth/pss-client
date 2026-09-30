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

from pss_client.auth import PssAuth, UserLogin
from pss_client.transport import PssApiError, PssHttpClient
from pss_client.ship import PssShipService
from pss_client.game import PssGameService, create_action_checksum

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
            session_token = f"sess_{login.user_id}_{login.access_token[:16]}"
            active_sessions[session_token] = login
            logger.info("[login] LOGIN OK user_id=%s session=%s", login.user_id, session_token)
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
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")
    return login


@app.post("/auth/logout")
async def logout(req: SessionRequest):
    """Cierra la sesión local (borra el token del dict en memoria)."""
    if req.session_token in active_sessions:
        del active_sessions[req.session_token]
        logger.info("[logout] sesión cerrada %s", req.session_token[:20])
    return {"success": True}


# --- User / cuenta ----------------------------------------------------------- #

@app.post("/user/me")
async def user_me(req: SessionRequest):
    """Datos de la cuenta autenticada (desde GetCurrentUser)."""
    login = _require_session(req.session_token)
    http = PssHttpClient()
    try:
        resp = await http.request(
            "UserService/GetCurrentUser",
            method="POST",
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
        ship = await ship_service.get_ship_by_user_id(
            access_token=login.access_token,
            user_id=login.user_id,
        )
        await http.close()
        return {
            "success": True,
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
                "characters_count": len(ship.characters),
                "rooms_count": len(ship.rooms),
                "items_count": len(ship.items),
            },
            "characters": [
                {
                    "character_id": c.character_id,
                    "name": c.name,
                    "level": c.level,
                    "hp": c.hp,
                    "max_hp": c.max_hp,
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
                    "level": r.level,
                    "hp": r.hp,
                    "max_hp": r.max_hp,
                    "power": r.power,
                    "max_power": r.max_power,
                    "capacity": r.capacity,
                    "character_ids": r.characters,
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
