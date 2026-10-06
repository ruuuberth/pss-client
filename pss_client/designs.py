"""Catálogo de diseños del juego (DesignService).

Flujo verificado (2026-10-01):
  1. SettingService/GetLatestVersion4 → Setting con las versiones actuales
     de todos los catálogos
  2. DesignService/ListAllStaticDesigns (params con las versiones del Setting)
     → habitaciones, personajes, naves, investigaciones (~13.5 MB XML)
  3. DesignService/ListAllDynamicDesigns (3 params: promotion/task/item)
     → misiones (TaskDesign) e items

Las respuestas de ListAll* son base64(gzip(XML)) — aunque el Content-Type
diga application/xml (pssapi lo documenta como response_gzipped). Con
versiones en 0 la API moderna devuelve el catálogo VACÍO: hay que pedir
primero las versiones actuales al Setting.

El catálogo se cachea en memoria (dicts id → nombre) por proceso.
"""

from __future__ import annotations

import base64
import gzip
import logging
import xml.etree.ElementTree as ET

from .transport import PssHttpClient

_log = logging.getLogger("pss_client.designs")

# ListAllStaticDesigns: param del endpoint -> atributo del <Setting>
_STATIC_PARAM_MAP = {
    "ListAchievementDesignVersion": "AchievementDesignVersion",
    "ListActionTypeVersion": "ActionTypeVersion",
    "ListAllChallengeDesignVersion": "ChallengeDesignVersion",
    "ListAllCharacterDesignActionVersion": "CharacterDesignActionVersion",
    "ListAllCharacterDesignVersion": "CharacterDesignVersion",
    "ListAllCollectionDesignVersion": "CollectionDesignVersion",
    "ListAllDivisionDesignVersion": "DivisionDesignVersion",
    "ListAllDrawDesignVersion": "DrawDesignVersion",
    "ListAllMissionDesignVersion": "MissionDesignVersion",
    "ListAllNewsDesignVersion": "NewsDesignVersion",
    "ListAllResearchDesignVersion": "ResearchDesignVersion",
    "ListAllRewardDesignVersion": "RewardDesignVersion",
    "ListAllShipDesignVersion": "ShipDesignVersion",
    "ListAllSituationDesignVersion": "SituationDesignVersion",
    "ListAllTrainingDesignVersion": "TrainingDesignVersion",
    "ListAnimationVersion": "AnimationVersion",
    "ListAssetVersion": "AssetVersion",
    "ListBackgroundVersion": "BackgroundVersion",
    "ListConditionTypeVersion": "ConditionTypeVersion",
    "ListCraftDesignVersion": "CraftDesignVersion",
    "ListFileVersion": "FileVersion",
    "ListItemDesignActionVersion": "ItemDesignActionVersion",
    "ListLeagueVersion": "LeagueVersion",
    "ListMarkerGeneratorDesignVersion": "MarkerGeneratorDesignVersion",
    "ListMissileDesignVersion": "MissileDesignVersion",
    "ListRoomDesignPurchaseVersion": "RoomDesignPurchaseVersion",
    "ListRoomDesignSpriteVersion": "RoomDesignSpriteVersion",
    "ListRoomDesignVersion": "RoomDesignVersion",
    "ListSeasonDesignVersion": "SeasonDesignVersion",
    "ListSkinSetVersion": "SkinSetVersion",
    "ListSkinVersion": "SkinVersion",
    "ListSpriteVersion": "SpriteVersion",
    "ListStarSystemLinkVersion": "StarSystemLinkVersion",
    "ListStarSystemVersion": "StarSystemVersion",
}

# caché por proceso: {coleccion: {id: nombre}}
_DESIGN_CACHE: dict[str, dict[int, str]] = {}


async def _get_designs_raw(http: PssHttpClient, path: str, params: dict) -> ET.Element:
    """GET crudo a DesignService decodificando base64(gzip(XML)).

    Usa HTTP directo (no el transport) porque la respuesta es base64-gzip
    y el transport rechazaría el contenido no-XML antes de decodificar.
    """
    import aiohttp
    from urllib.parse import urlencode
    url = f"https://{http.production_server}/{path}?{urlencode(params)}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = (await resp.read()).decode("utf-8", errors="replace")
    if data.lstrip().startswith("<"):
        # error devuelto como XML plano
        raise RuntimeError(f"{path}: {data[:150]}")
    return ET.fromstring(gzip.decompress(base64.b64decode(data)))


def _collect(root: ET.Element, tag: str, id_attr: str,
             name_attrs: tuple[str, ...]) -> dict[int, str]:
    out: dict[int, str] = {}
    for node in root.findall(f".//{tag}"):
        did = node.attrib.get(id_attr)
        if not did:
            continue
        name = None
        for na in name_attrs:
            name = node.attrib.get(na)
            if name:
                break
        try:
            out[int(did)] = name or f"{tag} #{did}"
        except ValueError:
            continue
    return out


async def get_design_catalog(http: PssHttpClient, access_token: str) -> dict[str, dict[int, str]]:
    """Devuelve {coleccion: {id: nombre}} con caché por proceso.

    Colecciones: room_designs, character_designs, ship_designs,
                 item_designs, task_designs, research_designs
    """
    if _DESIGN_CACHE:
        return _DESIGN_CACHE

    _log.info("[designs] descargando catálogo…")

    # PASO 1: versiones actuales del Setting
    resp = await http.request(
        "SettingService/GetLatestVersion4",
        method="GET",
        response_root_tag="SettingService",
        deviceType="DeviceTypeAndroid",
        languageKey="en",
    )
    setting = resp.root.find(".//Setting")
    if setting is None:
        raise RuntimeError("GetLatestVersion4 sin <Setting>")

    # PASO 2: catálogo estático (habitaciones, personajes, naves, investigación)
    static_params = {"LanguageKey": "en"}
    for param, key in _STATIC_PARAM_MAP.items():
        if key in setting.attrib:
            static_params[param] = setting.attrib[key]
    static_root = await _get_designs_raw(
        http, "DesignService/ListAllStaticDesigns", static_params)

    _DESIGN_CACHE.update({
        "room_designs": _collect(static_root, "RoomDesign", "RoomDesignId",
                                  ("RoomName",)),
        # stats por CharacterDesign para derivar max HP por nivel
        # (fórmula del juego: GetStatForLevel RVA 0x3853B30 — ver módulo ship):
        "character_stats": {
            int(cd.attrib.get("CharacterDesignId", 0)): {
                "name": cd.attrib.get("CharacterDesignName", ""),
                "hp": cd.attrib.get("Hp", "0"),
                "final_hp": cd.attrib.get("FinalHp", "0"),
                "max_level": cd.attrib.get("MaxCharacterLevel", "40"),
                "progression": cd.attrib.get("ProgressionType", "Linear"),
            }
            for cd in static_root.findall(".//CharacterDesign")
        },
        # atributos extra de RoomDesign para joins del dashboard:
        # tipo (Shield, Reactor, ...) y stats
        "room_types": _collect(static_root, "RoomDesign", "RoomDesignId",
                               ("RoomType",)),
        "room_stats": {
            int(rd.attrib.get("RoomDesignId", 0)): {
                "type": rd.attrib.get("RoomType", ""),
                "max_system_power": rd.attrib.get("MaxSystemPower", ""),
                "capacity": rd.attrib.get("Capacity", ""),
                "level": rd.attrib.get("Level", ""),
                "category": rd.attrib.get("CategoryType", ""),
            }
            for rd in static_root.findall(".//RoomDesign")
        },
        "character_designs": _collect(static_root, "CharacterDesign",
                                       "CharacterDesignId",
                                       ("CharacterDesignName",)),
        "ship_designs": _collect(static_root, "ShipDesign", "ShipDesignId",
                                  ("ShipDesignName",)),
        "research_designs": _collect(static_root, "ResearchDesign",
                                      "ResearchDesignId",
                                      ("ResearchDescription", "Description")),
    })

    # PASO 3: catálogo dinámico (misiones, items)
    dyn_params = {
        "LanguageKey": "en",
        "ListAllPromotionDesignVersion": setting.attrib.get("PromotionDesignVersion", "0"),
        "ListAllTaskDesignVersion": setting.attrib.get("TaskDesignVersion", "0"),
        "ListItemDesignVersion": setting.attrib.get("ItemDesignVersion", "0"),
    }
    dyn_root = await _get_designs_raw(
        http, "DesignService/ListAllDynamicDesigns", dyn_params)

    _DESIGN_CACHE.update({
        "task_designs": _collect(dyn_root, "TaskDesign", "TaskDesignId",
                                  ("Name", "Description")),
        "item_designs": _collect(dyn_root, "ItemDesign", "ItemDesignId",
                                  ("ItemDesignName",)),
    })

    _log.info("[designs] catálogo cargado: %s",
              {k: len(v) for k, v in _DESIGN_CACHE.items()})
    return _DESIGN_CACHE


def stat_for_level(level: int, basic: float, final: float,
                   max_level: int, progression: str = "Linear") -> float:
    """Replica PSCharacter.GetStatForLevel (RVA 0x3853B30) sin el bonus de gym.

    Fórmula desensamblada del juego:
        pct   = GetFinalPercentageForLevel(level)   # según ProgressionType
        stat  = (pct * final + (1 - pct) * basic) * (1 + bonus/100)
    El bonus (entrenamiento/gym, BoostValuesString) no se aplica aquí: es
    estado por-usuario que el XML de la nave no expone.

    ProgressionType (verificado en el catálogo: Linear 489, EaseIn 48,
    EaseOut 17):
        Linear : pct = (L-1)/(maxLevel-1)
        EaseIn : pct = ((L-1)/(maxLevel-1))^2      # case 1 del switch
        EaseOut: 1 - (1-(L-1)/(maxLevel-1))^2      # case 2 (pow con 2.0)
    """
    if max_level <= 1:
        return float(basic)
    x = (level - 1) / (max_level - 1)
    if progression == "EaseIn":
        pct = x * x
    elif progression == "EaseOut":
        pct = 1 - (1 - x) * (1 - x)
    else:  # Linear
        pct = x
    return pct * float(final) + (1 - pct) * float(basic)
