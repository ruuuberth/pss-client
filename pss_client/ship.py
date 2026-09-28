"""Servicios de nave (ship) para la API de Pixel Starships."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, List

from .transport import PssHttpClient, PssApiError


@dataclass
class Character:
    character_id: int
    character_design_id: int
    name: str
    hp: float
    max_hp: float
    shield: int
    max_shield: int
    room_id: int
    level: int
    race_id: int
    weapon_design_id: int
    ability_design_id: int
    current_room_id: int
    sprite_design_id: int
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_xml(cls, node: ET.Element) -> "Character":
        return cls(
            character_id=int(node.attrib.get("CharacterId", 0)),
            character_design_id=int(node.attrib.get("CharacterDesignId", 0)),
            name=node.attrib.get("Name", ""),
            hp=float(node.attrib.get("HP", 0)),
            max_hp=float(node.attrib.get("MaxHP", 0)),
            shield=int(node.attrib.get("Shield", 0)),
            max_shield=int(node.attrib.get("MaxShield", 0)),
            room_id=int(node.attrib.get("RoomId", 0)),
            level=int(node.attrib.get("Level", 0)),
            race_id=int(node.attrib.get("RaceId", 0)),
            weapon_design_id=int(node.attrib.get("WeaponDesignId", 0)),
            ability_design_id=int(node.attrib.get("AbilityDesignId", 0)),
            current_room_id=int(node.attrib.get("CurrentRoomId", 0)),
            sprite_design_id=int(node.attrib.get("SpriteDesignId", 0)),
            raw=dict(node.attrib),
        )


@dataclass
class Room:
    room_id: int
    room_design_id: int
    level: int
    hp: float
    max_hp: float
    power: int
    max_power: int
    capacity: int
    characters: List[int] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_xml(cls, node: ET.Element) -> "Room":
        char_ids = []
        chars = node.find("Characters")
        if chars is not None:
            char_ids = [int(c.attrib.get("CharacterId", 0)) for c in chars.findall("Character")]
        return cls(
            room_id=int(node.attrib.get("RoomId", 0)),
            room_design_id=int(node.attrib.get("RoomDesignId", 0)),
            level=int(node.attrib.get("Level", 0)),
            hp=float(node.attrib.get("HP", 0)),
            max_hp=float(node.attrib.get("MaxHP", 0)),
            power=int(node.attrib.get("Power", 0)),
            max_power=int(node.attrib.get("MaxPower", 0)),
            capacity=int(node.attrib.get("Capacity", 0)),
            characters=char_ids,
            raw=dict(node.attrib),
        )


@dataclass
class Item:
    item_id: int
    item_design_id: int
    amount: int
    room_id: int
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_xml(cls, node: ET.Element) -> "Item":
        return cls(
            item_id=int(node.attrib.get("ItemId", 0)),
            item_design_id=int(node.attrib.get("ItemDesignId", 0)),
            amount=int(node.attrib.get("Amount", 0)),
            room_id=int(node.attrib.get("RoomId", 0)),
            raw=dict(node.attrib),
        )


@dataclass
class Ship:
    """Información de la nave del jugador."""
    ship_id: int
    ship_name: str
    ship_level: int
    ship_design_id: int
    ship_status: str
    user_id: int
    hp: float
    shield: int
    power_score: int
    star_system_id: int
    next_star_system_id: int
    center_x: int
    center_y: int
    top_left_x: int
    top_left_y: int
    brightness_value: float
    hue_value: float
    saturation_value: float
    skin_item_design_id: int
    skin_opacity_value: float
    sticker_string: str
    tags: str
    characters: List[Character] = field(default_factory=list)
    rooms: List[Room] = field(default_factory=list)
    items: List[Item] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_xml(cls, node: ET.Element) -> "Ship":
        chars = [Character.from_xml(c) for c in node.findall(".//Character")]
        rooms = [Room.from_xml(r) for r in node.findall(".//Room")]
        items = [Item.from_xml(i) for i in node.findall(".//Item")]
        return cls(
            ship_id=int(node.attrib.get("ShipId", 0)),
            ship_name=node.attrib.get("ShipName", ""),
            ship_level=int(node.attrib.get("ShipLevel", 0)),
            ship_design_id=int(node.attrib.get("ShipDesignId", 0)),
            ship_status=node.attrib.get("ShipStatus", ""),
            user_id=int(node.attrib.get("UserId", 0)),
            hp=float(node.attrib.get("HP", 0)),
            shield=int(node.attrib.get("Shield", 0)),
            power_score=int(node.attrib.get("PowerScore", 0)),
            star_system_id=int(node.attrib.get("StarSystemId", 0)),
            next_star_system_id=int(node.attrib.get("NextStarSystemId", 0)),
            center_x=int(node.attrib.get("CenterX", 0)),
            center_y=int(node.attrib.get("CenterY", 0)),
            top_left_x=int(node.attrib.get("TopLeftX", 0)),
            top_left_y=int(node.attrib.get("TopLeftY", 0)),
            brightness_value=float(node.attrib.get("BrightnessValue", 0)),
            hue_value=float(node.attrib.get("HueValue", 0)),
            saturation_value=float(node.attrib.get("SaturationValue", 0)),
            skin_item_design_id=int(node.attrib.get("SkinItemDesignId", 0)),
            skin_opacity_value=float(node.attrib.get("SkinOpacityValue", 0)),
            sticker_string=node.attrib.get("StickerString", ""),
            tags=node.attrib.get("Tags", ""),
            characters=chars,
            rooms=rooms,
            items=items,
            raw=dict(node.attrib),
        )


class PssShipService:
    """Servicio para obtener información de naves."""

    def __init__(self, http_client: PssHttpClient):
        self._http = http_client

    async def get_ship_by_user_id(
        self,
        access_token: str,
        user_id: int,
        client_datetime: Optional[datetime] = None,
    ) -> Ship:
        """GET ShipService/GetShipByUserId - Obtiene la nave principal del usuario."""
        from datetime import timezone
        client_datetime = client_datetime or datetime.now(timezone.utc)
        cdt = client_datetime.strftime("%Y-%m-%dT%H:%M:%S")

        resp = await self._http.request(
            "ShipService/GetShipByUserId",
            method="GET",
            response_root_tag="ShipService",
            accessToken=access_token,
            clientDateTime=cdt,
            userId=user_id,
        )
        ship_node = resp.root.find(".//Ship")
        if ship_node is None:
            raise PssApiError("No se encontró nave en la respuesta")
        return Ship.from_xml(ship_node)

    async def inspect_ship(
        self,
        access_token: str,
        user_id: int,
    ) -> tuple[Ship, dict]:
        """GET ShipService/InspectShip2 - Inspecciona nave (devuelve Ship + User)."""
        resp = await self._http.request(
            "ShipService/InspectShip2",
            method="GET",
            response_root_tag="ShipService",
            accessToken=access_token,
            userId=user_id,
        )
        ship_node = resp.root.find(".//Ship")
        user_node = resp.root.find(".//User")
        if ship_node is None:
            raise PssApiError("No se encontró nave en InspectShip")
        ship = Ship.from_xml(ship_node)
        user_data = dict(user_node.attrib) if user_node is not None else {}
        return ship, user_data


def parse_ship_xml(xml_string: str) -> Ship:
    """Parsea XML de nave directamente (útil para debug/cassettes)."""
    root = ET.fromstring(xml_string)
    ship_node = root.find(".//Ship")
    if ship_node is None:
        raise PssApiError("No hay nodo Ship en el XML")
    return Ship.from_xml(ship_node)