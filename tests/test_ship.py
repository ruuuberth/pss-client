"""Unit tests del parser de ship.py (sin red).

Cubren la calibración del issue #7: el bonus de gym entra al max HP como
  round(GetStatForLevel(level, Hp, FinalHp, digits=1)) * (100 + HpImprovement) // 100
fórmula desensamblada de PSCharacter.GetMaxHPForLevel (RVA 0x3853E20).

Uso:
    uv run pytest tests/test_ship.py -q
"""

import xml.etree.ElementTree as ET

from pss_client.designs import max_hp_for
from pss_client.ship import Character


def _char_xml(**attrs: str) -> ET.Element:
    node = ET.Element("Character")
    for key, value in attrs.items():
        node.set(key, value)
    return node


def test_character_parses_improvement_fields():
    node = _char_xml(
        CharacterId="76237890",
        CharacterDesignId="241",
        CharacterName="Ruuuberth75",
        Level="40",
        HpImprovement="22",
        AttackImprovement="3",
        PilotImprovement="4",
        RepairImprovement="5",
        ScienceImprovement="107",
        EngineImprovement="6",
        WeaponImprovement="7",
        StaminaImprovement="15",
        AbilityImprovement="8",
        BoostLevel="2",
        Fatigue="24",
    )
    c = Character.from_xml(node)
    assert c.hp_improvement == 22
    assert c.attack_improvement == 3
    assert c.pilot_improvement == 4
    assert c.repair_improvement == 5
    assert c.science_improvement == 107
    assert c.engine_improvement == 6
    assert c.weapon_improvement == 7
    assert c.stamina_improvement == 15
    assert c.ability_improvement == 8
    assert c.boost_level == 2
    assert c.fatigue == 24


def test_character_improvement_fields_default_to_zero():
    node = _char_xml(CharacterId="1", CharacterDesignId="2", Level="5")
    c = Character.from_xml(node)
    assert c.hp_improvement == 0
    assert c.science_improvement == 0
    assert c.boost_level == 0
    assert c.fatigue == 0


def test_max_hp_bonus_scale_matches_getmaxhpforlevel():
    """round(base) * (100+g) // 100 — el entero-truncado de (100+g)/100."""
    # el redondeo del base ocurre antes de aplicar el bonus
    assert max_hp_for(6.6, 20) == 8  # round(6.6)=7; 7*1.2 truncado = 8
    assert max_hp_for(6.3, 22) == 7  # g=22 sobre base 6
    # sin gym: bonus nulo, el valor no cambia
    assert max_hp_for(6.6, 0) == 7
    # mejora pequeña: por debajo del umbral de 1 HP por el redondeo a enteros
    assert max_hp_for(6.3, 4) == 6  # g=4 → 6.24 → 6
    assert max_hp_for(25.0, 4) == 26  # base alta: g=4 sí mueve el entero
