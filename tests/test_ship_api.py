"""Tests offline del enriquecimiento de /ship/info (sin red)."""

import asyncio
from types import SimpleNamespace

import pss_client.api as api_module
from pss_client.auth import UserLogin


class FakeHttp:
    async def close(self):
        pass


class FakeShipService:
    def __init__(self, ship):
        self._ship = ship

    async def get_ship_by_user_id(self, access_token, user_id):
        return self._ship


def _ship_with_character(character, rooms=None):
    return SimpleNamespace(
        ship_id=1,
        ship_name="Test",
        ship_level=10,
        ship_design_id=20,
        ship_status="Online",
        hp=0,
        shield=0,
        power_score=100,
        star_system_id=1,
        next_star_system_id=2,
        center_x=0,
        center_y=0,
        skin_item_design_id=0,
        skin_opacity_value=0,
        sticker_string="",
        tags="",
        immunity_date=None,
        status_start_date=None,
        update_date=None,
        characters=[character],
        rooms=rooms or [],
        items=[],
    )


def _character(hp_improvement=0):
    return SimpleNamespace(
        character_id=100,
        character_design_id=10,
        name="Ninja",
        level=33,
        hp=0,
        max_hp=0,
        hp_improvement=hp_improvement,
        shield=0,
        race_id=1,
        weapon_design_id=0,
        ability_design_id=0,
        room_id=1,
    )


def _get_ship_info(monkeypatch, ship, catalog):
    monkeypatch.setitem(
        api_module.active_sessions,
        "test",
        UserLogin(access_token="token", user_id=42, raw={}),
    )
    monkeypatch.setattr(api_module, "PssHttpClient", FakeHttp)
    monkeypatch.setattr(
        api_module, "PssShipService", lambda http: FakeShipService(ship)
    )

    async def fake_catalog(http, access_token):
        return catalog

    monkeypatch.setattr(api_module, "get_design_catalog", fake_catalog)
    return asyncio.run(
        api_module.get_ship_info(api_module.SessionRequest(session_token="test"))
    )


def test_base_max_hp_conserva_el_base_antes_del_bonus(monkeypatch):
    catalog = {
        "character_stats": {
            10: {
                "hp": "6",
                "final_hp": "6.4",
                "max_level": "40",
                "progression": "Linear",
            }
        },
        "room_designs": {},
        "room_types": {},
        "room_stats": {},
    }

    result = _get_ship_info(monkeypatch, _ship_with_character(_character(20)), catalog)

    enriched = result["characters"][0]
    assert enriched["max_hp"] == 7
    assert enriched["base_max_hp"] == 6.3
    assert enriched["hp_gym_bonus"] == 20


def test_max_shield_suma_capacity_de_las_salas_shield(monkeypatch):
    rooms = [
        SimpleNamespace(
            room_id=room_id,
            room_design_id=room_id,
            room_status="Activate",
            assigned_power=0,
            system_power=0,
            current_capacity=0,
            characters=[],
        )
        for room_id in (2, 1)
    ]
    catalog = {
        "character_stats": {},
        "room_designs": {1: "Capacitor", 2: "Overcharger"},
        "room_types": {1: "Shield", 2: "Shield"},
        "room_stats": {
            1: {"type": "Shield", "capacity": "2"},
            2: {"type": "Shield", "capacity": "3"},
        },
    }

    result = _get_ship_info(
        monkeypatch, _ship_with_character(_character(), rooms), catalog
    )

    assert result["shield_info"]["max_shield"] == 500
    assert result["ship"]["max_shield"] == 500
    assert [room["room_id"] for room in result["shield_info"]["rooms"]] == [1, 2]
