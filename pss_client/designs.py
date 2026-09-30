"""Catálogo de diseños del juego (DesignService).

ListAllStaticDesigns devuelve el catálogo COMPLETO del juego (habitaciones,
personajes, naves, items, tareas...) en un XML comprimido:
  respuesta = base64(gzip(XML))

El catálogo pesa varios MB y no cambia entre sesiones, así que se cachea en
memoria (dicts id → nombre) una sola vez por proceso.

Campos clave (verificados contra pssapi entities/raw):
  RoomDesign:      RoomDesignId -> RoomName / RoomShortName
  CharacterDesign: CharacterDesignId -> CharacterDesignName
  ShipDesign:      ShipDesignId -> ShipDesignName
  ItemDesign:      ItemDesignId -> ItemDesignName
  TaskDesign:      TaskDesignId -> TaskDescription
"""

from __future__ import annotations

import base64
import gzip
import logging
import xml.etree.ElementTree as ET
from typing import Any, Optional

from .transport import PssApiError, PssHttpClient

_log = logging.getLogger("pss_client.designs")

# Todas las versiones de catálogo en 0 = "dame todo"
_ALL_VERSION_PARAMS = {
    "ListAchievementDesignVersion": 0,
    "ListActionTypeVersion": 0,
    "ListAllChallengeDesignVersion": 0,
    "ListAllCharacterDesignActionVersion": 0,
    "ListAllCharacterDesignVersion": 0,
    "ListAllCollectionDesignVersion": 0,
    "ListAllDivisionDesignVersion": 0,
    "ListAllDrawDesignVersion": 0,
    "ListAllMissionDesignVersion": 0,
    "ListAllNewsDesignVersion": 0,
    "ListAllResearchDesignVersion": 0,
    "ListAllRewardDesignVersion": 0,
    "ListAllShipDesignVersion": 0,
    "ListAllSituationDesignVersion": 0,
    "ListAllTrainingDesignVersion": 0,
    "ListAnimationVersion": 0,
    "ListAssetVersion": 0,
    "ListBackgroundVersion": 0,
    "ListConditionTypeVersion": 0,
    "ListCraftDesignVersion": 0,
    "ListFileVersion": 0,
    "ListItemDesignActionVersion": 0,
    "ListLeagueVersion": 0,
    "ListMarkerGeneratorDesignVersion": 0,
    "ListMissileDesignVersion": 0,
    "ListRoomDesignPurchaseVersion": 0,
    "ListRoomDesignSpriteVersion": 0,
    "ListRoomDesignVersion": 0,
    "ListSeasonDesignVersion": 0,
    "ListSkinSetVersion": 0,
    "ListSkinVersion": 0,
    "ListSpriteVersion": 0,
    "ListStarSystemLinkVersion": 0,
    "ListStarSystemVersion": 0,
}

# caché por proceso: {nombre_coleccion: {id: nombre}}
_DESIGN_CACHE: dict[str, dict[int, str]] = {}


def _decode_payload(data: str) -> ET.Element:
    """La respuesta es base64(gzip(XML))."""
    raw = gzip.decompress(base64.b64decode(data))
    return ET.fromstring(raw)


def _collect(
    root: ET.Element,
    tag: str,
    id_attr: str,
    name_attrs: tuple[str, ...],
) -> dict[int, str]:
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

    _log.info("[designs] descargando catálogo (ListAllStaticDesigns)...")
    resp = await http.request(
        "DesignService/ListAllStaticDesigns",
        method="GET",
        response_root_tag="DesignService",
        LanguageKey="en",
        DeviceType="DeviceTypeWeb",
        accessToken=access_token,
        **{k: str(v) for k, v in _ALL_VERSION_PARAMS.items()},
    )
    # el transport devuelve el XML ya parseado — pero este endpoint responde
    # base64(gzip). Detectar ambos formatos:
    root = resp.root
    if root.tag == "ListAllDesigns" and len(list(root)) == 0:
        # el transport parseó el base64 como texto plano -> re-decodificar
        root = _decode_payload(resp.raw)

    _DESIGN_CACHE.update({
        "room_designs": _collect(root, "RoomDesign", "RoomDesignId",
                                  ("RoomShortName", "RoomName")),
        "character_designs": _collect(root, "CharacterDesign", "CharacterDesignId",
                                      ("CharacterDesignName", "CharacterName")),
        "ship_designs": _collect(root, "ShipDesign", "ShipDesignId",
                                 ("ShipDesignName", "ShipName")),
        "item_designs": _collect(root, "ItemDesign", "ItemDesignId",
                                  ("ItemDesignName", "ItemName", "Description")),
        "task_designs": _collect(root, "TaskDesign", "TaskDesignId",
                                 ("TaskDescription", "Description")),
        "research_designs": _collect(root, "ResearchDesign", "ResearchDesignId",
                                     ("ResearchDescription", "Description")),
    })
    _log.info("[designs] catálogo cargado: %s",
              {k: len(v) for k, v in _DESIGN_CACHE.items()})
    return _DESIGN_CACHE
