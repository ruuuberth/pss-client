"""Servicios de JUEGO para la API de Pixel Starships.

Endpoints de acción y consulta para "jugar" desde el dashboard:

Lectura (solo accessToken):
  - TaskService/ListTasksOfAUser        → misiones/tareas diarias
  - ResearchService/ListAllResearches   → investigaciones disponibles
  - CharacterService/ListAllCharactersOfUser → personajes
  - RoomService/ListRoomsViaAccessToken → habitaciones
  - GalaxyService/ListUserStarSystems   → sistemas estelares visitados

Acción (checksum genérico):
  - RoomService/CollectResources3  → recolectar minerales de una habitación
  - RoomService/Refill2            → rellenar armas/módulos
  - UserService/CollectDailyReward2 → recompensa diaria

Fórmula del checksum genérico (verificada contra GetEngagement en pssapi
y el patrón SavysodaEncryptString del desensamblado):
  checksum = md5(clientDateTime + checksum_key + savy_checksum)
donde checksum_key="5343" y savy_checksum="Savvy!s0d@" (o env vars).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .transport import PssApiError, PssHttpClient

_log = logging.getLogger("pss_client.game")

from .auth import CHECKSUM_KEY, SAVY_CHECKSUM  # respetan env vars


def create_action_checksum(
    client_datetime: Optional[datetime] = None,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Checksum genérico para endpoints de acción (Refill2, CollectDailyReward2, ...).

    md5(timestamp + checksum_key + savy_checksum)
    """
    dt = client_datetime or datetime.now(timezone.utc)
    timestamp = dt.strftime("%Y-%m-%dT%H:%M:%S")
    payload = f"{timestamp}{checksum_key}{savy_checksum}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Task:
    task_id: int
    task_design_id: int
    progress_value: float
    collected: bool
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_xml(cls, node) -> "Task":
        return cls(
            task_id=int(node.attrib.get("TaskId", 0)),
            task_design_id=int(node.attrib.get("TaskDesignId", 0)),
            progress_value=float(node.attrib.get("ProgressValue", 0) or 0),
            collected=node.attrib.get("Collected", "false").lower() == "true",
            raw=dict(node.attrib),
        )


@dataclass
class Research:
    research_id: int
    user_id: int
    research_design_id: int
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_xml(cls, node) -> "Research":
        return cls(
            research_id=int(node.attrib.get("ResearchId", 0)),
            user_id=int(node.attrib.get("UserId", 0)),
            research_design_id=int(node.attrib.get("ResearchDesignId", 0)),
            raw=dict(node.attrib),
        )


@dataclass
class StarSystem:
    star_system_id: int
    user_id: int
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_xml(cls, node) -> "StarSystem":
        return cls(
            star_system_id=int(node.attrib.get("StarSystemId", 0)),
            user_id=int(node.attrib.get("UserId", 0)),
            raw=dict(node.attrib),
        )


class PssGameService:
    """Consultas y acciones de juego."""

    def __init__(self, http_client: PssHttpClient):
        self._http = http_client

    # ------------------------- LECTURA ------------------------- #

    async def list_tasks(self, access_token: str) -> list[Task]:
        """GET TaskService/ListTasksOfAUser — tareas/misiones diarias."""
        resp = await self._http.request(
            "TaskService/ListTasksOfAUser",
            method="GET",
            response_root_tag="TaskService",
            accessToken=access_token,
        )
        return [Task.from_xml(n) for n in resp.root.findall(".//Task")]

    async def list_researches(self, access_token: str) -> list[Research]:
        """GET ResearchService/ListAllResearches — investigaciones del usuario."""
        resp = await self._http.request(
            "ResearchService/ListAllResearches",
            method="GET",
            response_root_tag="ResearchService",
            accessToken=access_token,
        )
        return [Research.from_xml(n) for n in resp.root.findall(".//Research")]

    async def list_star_systems(self, access_token: str) -> list[StarSystem]:
        """GET GalaxyService/ListUserStarSystems — sistemas estelares del usuario."""
        resp = await self._http.request(
            "GalaxyService/ListUserStarSystems",
            method="GET",
            response_root_tag="GalaxyService",
            accessToken=access_token,
        )
        return [StarSystem.from_xml(n) for n in resp.root.findall(".//StarSystem")]

    async def list_characters(self, access_token: str) -> list[dict]:
        """GET CharacterService/ListAllCharactersOfUser — personajes completos."""
        resp = await self._http.request(
            "CharacterService/ListAllCharactersOfUser",
            method="GET",
            response_root_tag="CharacterService",
            accessToken=access_token,
        )
        return [dict(n.attrib) for n in resp.root.findall(".//Character")]

    async def list_rooms(self, access_token: str) -> list[dict]:
        """GET RoomService/ListRoomsViaAccessToken — habitaciones con estado."""
        resp = await self._http.request(
            "RoomService/ListRoomsViaAccessToken",
            method="GET",
            response_root_tag="RoomService",
            accessToken=access_token,
        )
        return [dict(n.attrib) for n in resp.root.findall(".//Room")]

    # ------------------------- ACCIÓN ------------------------- #

    async def collect_room_resources(
        self,
        access_token: str,
        room_id: int,
        amount: int,
        collect_date: Optional[str] = None,
    ) -> dict:
        """GET RoomService/CollectResources3 — recolecta minerales de una habitación.

        URL del juego: ?roomId={0}&amount={1}&collectDate={2}&checksum={3}&accessToken={4}
        checksum genérico = md5(clientDateTime + key + savy).
        """
        cdt = collect_date or _now_ts()
        checksum = create_action_checksum()
        resp = await self._http.request(
            "RoomService/CollectResources3",
            method="GET",
            response_root_tag="RoomService",
            roomId=room_id,
            amount=amount,
            collectDate=cdt,
            checksum=checksum,
            accessToken=access_token,
        )
        _log.info("[collect-resources] roomId=%s amount=%s OK", room_id, amount)
        return {"success": True, "raw": resp.raw[:500]}

    async def collect_daily_reward(self, access_token: str) -> dict:
        """GET UserService/CollectDailyReward2 — recompensa diaria."""
        cdt = _now_ts()
        checksum = create_action_checksum()
        resp = await self._http.request(
            "UserService/CollectDailyReward2",
            method="GET",
            response_root_tag="UserService",
            clientDateTime=cdt,
            checksum=checksum,
            accessToken=access_token,
        )
        _log.info("[collect-daily-reward] OK")
        return {"success": True, "raw": resp.raw[:500]}
