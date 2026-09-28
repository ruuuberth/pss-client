"""API REST wrapper para pss_client - permite usar el backend desde un frontend web."""

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

from pss_client.auth import PssAuth, create_checksum, CHECKSUM_KEY, SAVY_CHECKSUM
from pss_client.transport import PssApiError
from pss_client.ship import PssShipService

# --- Logging: archivo + consola, con rotación -------------------------------- #
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

app = FastAPI(title="PSS API Gateway", version="0.1.0")

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

@app.get("/debug/path")
async def debug_path():
    return {
        "frontend_dir": frontend_dir,
        "index_exists": os.path.exists(os.path.join(frontend_dir, "index.html")),
        "cwd": os.getcwd()
    }

# Estado global de sesiones autenticadas (en producción usar Redis/DB)
active_sessions = {}


class DeviceLoginRequest(BaseModel):
    device_key: str


class EmailPasswordRequest(BaseModel):
    email: Optional[str] = None  # opcional: el login real usa deviceKey+password
    password: str
    device_key: Optional[str] = None
    version: int = 4
    device_type: str = "DeviceTypeAndroid"


class SteamLoginRequest(BaseModel):
    ticket: str
    device_key: Optional[str] = None


class ChecksumRequest(BaseModel):
    device_key: str
    device_type: str = "DeviceTypeAndroid"


@app.post("/auth/device-login")
async def device_login(req: DeviceLoginRequest):
    """Login con device_key existente (cuenta ya ligada al dispositivo)."""
    async with PssAuth() as auth:
        try:
            login = await auth.device_login(device_key=req.device_key)
            session_token = f"sess_{login.user_id}_{login.access_token[:16]}"
            active_sessions[session_token] = login
            return {
                "success": True,
                "session_token": session_token,
                "user_id": login.user_id,
                "access_token": login.access_token,
                "previous_last_login": login.previous_last_login_date,
            }
        except PssApiError as e:
            # Detectar el caso "cuenta verificada vinculada" (JSON con reason)
            import json as _json
            try:
                detail = _json.loads(str(e))
                if detail.get("reason") == "linked_account":
                    return {
                        "success": False,
                        "reason": "linked_account",
                        "email": detail.get("email"),
                        "user_id": detail.get("user_id"),
                        "message": "Tu cuenta está verificada y vinculada a email/Steam. "
                                   "Usa la pestaña 'Email + Pass' con tu contraseña, o 'Steam' con tu ticket.",
                    }
            except (ValueError, KeyError):
                pass
            raise HTTPException(status_code=401, detail=str(e))


@app.post("/auth/email-password")
async def email_password(req: EmailPasswordRequest):
    """Login con email y contraseña."""
    import hashlib as _h
    logger.info("[email-password] intento login email=%s device_key_provided=%s",
                req.email, bool(req.device_key))
    device_key = req.device_key
    async with PssAuth() as auth:
        try:
            login = await auth.email_password_login(
                email=req.email or "",
                password=req.password,
                device_key=device_key,
            )
            session_token = f"sess_{login.user_id}_{login.access_token[:16]}"
            active_sessions[session_token] = login
            logger.info("[email-password] LOGIN OK user_id=%s session=%s",
                        login.user_id, session_token)
            return {
                "success": True,
                "session_token": session_token,
                "user_id": login.user_id,
                "access_token": login.access_token,
            }
        except PssApiError as e:
            msg = str(e)
            logger.warning("[email-password] FALLO email=%s error=%r", req.email, msg)
            if "Email or password is incorrect" in msg or "Email o contraseña incorrectos" in msg:
                msg = "Email o contraseña incorrectos. Verifica tus credenciales de Pixel Starships."
            elif "An error occurred" in msg:
                msg = ("El servidor rechazó el login. Verifica email y contraseña. "
                       "(Si tu cuenta es solo-Steam, usa la pestaña Steam).")
            elif "Authorize falló" in msg:
                msg = ("Authorize falló (sin mensaje del servidor). Revisa pss-client.log "
                       "para ver la respuesta cruda de la API.")
            raise HTTPException(status_code=401, detail=msg)


@app.post("/auth/steam-login")
async def steam_login(req: SteamLoginRequest):
    """Login con ticket de sesión Steam."""
    async with PssAuth() as auth:
        try:
            login = await auth.steam_login(
                ticket=req.ticket,
                device_key=req.device_key,
            )
            session_token = f"sess_{login.user_id}_{login.access_token[:16]}"
            active_sessions[session_token] = login
            return {
                "success": True,
                "session_token": session_token,
                "user_id": login.user_id,
                "access_token": login.access_token,
            }
        except PssApiError as e:
            raise HTTPException(status_code=401, detail=str(e))


@app.post("/auth/checksum")
async def generate_checksum(req: ChecksumRequest):
    """Genera checksum para debug/pruebas."""
    from datetime import datetime, timezone
    checksum = create_checksum(
        device_key=req.device_key,
        client_datetime=datetime.now(timezone.utc),
        device_type=req.device_type,
    )
    return {
        "checksum": checksum,
        "checksum_key": CHECKSUM_KEY,
        "savy_checksum": SAVY_CHECKSUM,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "pss-api-gateway"}


# --- Ship endpoints ---

class ShipInfoRequest(BaseModel):
    session_token: str


@app.post("/ship/info")
async def get_ship_info(req: ShipInfoRequest):
    """Obtiene la información de la nave del usuario autenticado."""
    login = active_sessions.get(req.session_token)
    if not login:
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")

    try:
        ship_service = PssShipService(login._http if hasattr(login, '_http') else None)
        # Crear cliente HTTP temporal con el access_token
        from pss_client.transport import PssHttpClient
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
        }
    except PssApiError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno: {e}")


@app.post("/ship/inspect")
async def inspect_ship(req: ShipInfoRequest):
    """Inspecciona la nave (devuelve nave + info de usuario)."""
    login = active_sessions.get(req.session_token)
    if not login:
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")

    try:
        from pss_client.transport import PssHttpClient
        http = PssHttpClient()
        ship_service = PssShipService(http)
        ship, user_data = await ship_service.inspect_ship(
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
                "ship_status": ship.ship_status,
                "hp": ship.hp,
                "shield": ship.shield,
                "power_score": ship.power_score,
            },
            "user": user_data,
        }
    except PssApiError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)