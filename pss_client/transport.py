"""Transporte HTTP base para la API de Pixel Starships.

Replica el comportamiento verificado del cliente oficial (vía cassettes de
pssapi.py y respuestas reales):
- POST con query params + JSON body (DeviceLogin17, SteamLogin8, Authorize4)
- GET con query params (SettingService/GetLatestVersion3)
- Respuesta XML (raíz <NombreServicio>); errores como atributo errorMessage=""
- El servidor real responde 200 incluso en errores -> se parsea el XML.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

DEFAULT_PRODUCTION_SERVER = "api.pixelstarships.com"
DATETIME_FORMAT_ISO = "%Y-%m-%dT%H:%M:%S"

_log = logging.getLogger("pss_client.transport")


class PssApiError(Exception):
    """Error devuelto por la API (atributo errorMessage del XML)."""


@dataclass
class PssResponse:
    root: ET.Element
    raw: str


@dataclass
class Session:
    """Estado de autenticación de una cuenta."""

    access_token: str
    user_id: int
    previous_last_login_date: Optional[str] = None
    raw: dict[str, str] = field(default_factory=dict)


def _serialize_params(params: dict[str, Any]) -> dict[str, str]:
    filtered: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, datetime):
            filtered[key] = value.strftime(DATETIME_FORMAT_ISO)
        elif isinstance(value, bool):
            filtered[key] = str(value).lower()
        else:
            filtered[key] = str(value)
    return filtered


class PssHttpClient:
    """Cliente HTTP mínimo para la API de PSS (una sesión aiohttp reutilizable)."""

    def __init__(self, production_server: Optional[str] = None, timeout: int = 30):
        self._production_server = production_server or DEFAULT_PRODUCTION_SERVER
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def production_server(self) -> str:
        return self._production_server

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(
        self,
        path: str,
        method: str = "POST",
        json_body: Optional[dict[str, Any]] = None,
        response_root_tag: Optional[str] = None,
        **params: Any,
    ) -> PssResponse:
        """Ejecuta una request y devuelve el XML parseado.

        `response_root_tag` es el tag esperado de la raíz (ej. "UserService").
        Si el XML raíz trae errorMessage, lanza PssApiError.
        """
        query = _serialize_params(params)
        url = f"https://{self._production_server}/{path}"
        session = await self._ensure_session()

        kwargs: dict[str, Any] = {"params": query}
        if json_body is not None:
            kwargs["json"] = json_body

        async with session.request(method, url, **kwargs) as resp:
            data = await resp.text()

        # Log de cada request/response (nivel DEBUG; sube a INFO para ver todo)
        _log.debug("[transport] %s %s -> %d bytes: %s", method, path, len(data), data[:300])
        if not data or not data.lstrip().startswith("<"):
            _log.error("[transport] respuesta no-XML de %s: %r", path, data[:300])
            raise PssApiError(f"Respuesta no-XML de {path}: {data[:200]!r}")

        root = ET.fromstring(data)
        if root.tag.startswith("{http://www.w3.org/1999/xhtml}html"):
            raise PssApiError(f"Error del servidor en {path}: {data[:200]!r}")
        err = root.attrib.get("errorMessage")
        if err:
            # Caso especial: la raíz ES la entidad (ej. <UserLogin errorCode="400"
            # errorMessage="email@...">) con un <User> hijo con los datos de la
            # cuenta. Es una respuesta estructurada (cuenta vinculada), no un error
            # plano: la delegamos al parser específico del servicio.
            if root.attrib.get("errorCode") and root.find(".//User") is not None:
                _log.info("[transport] %s: respuesta estructurada (errorCode=%s)",
                          path, root.attrib.get("errorCode"))
                return PssResponse(root=root, raw=data)
            _log.warning("[transport] %s: errorMessage=%r XML=%s", path, err, data[:300])
            raise PssApiError(err)

        if response_root_tag and root.tag != response_root_tag:
            inner = root.find(f".//{response_root_tag}")
            if inner is None:
                raise PssApiError(
                    f"Tag esperado {response_root_tag!r} no encontrado; raíz={root.tag!r}"
                )
        return PssResponse(root=root, raw=data)

    async def get_production_server(self, device_type: str, language_key: str) -> str:
        """SettingService/GetLatestVersion3 -> ProductionServer vigente."""
        resp = await self.request(
            "SettingService/GetLatestVersion3",
            method="GET",
            response_root_tag="SettingService",
            deviceType=device_type,
            languageKey=language_key,
        )
        setting = resp.root.find(".//Setting")
        if setting is None or not setting.attrib.get("ProductionServer"):
            raise PssApiError("No se pudo determinar el ProductionServer")
        return setting.attrib["ProductionServer"]
