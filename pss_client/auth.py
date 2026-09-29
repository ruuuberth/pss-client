"""Backend de autenticación para la API de Pixel Starships.

Fórmula del checksum VERIFICADA contra la API real (login exitoso 2026-09-23),
derivada del desensamblado del cliente oficial:

    UserManager.DownloadUserLogin (RVA 0x36F6370):
        data = String.Concat(device_key, client_date_time, device_type, ChecksumKey)
    StringExtensions.SavysodaEncryptString (RVA 0x3822490):
        checksum = Md5Sum(data + SavyChecksum)

Por lo tanto:

    checksum = md5(device_key + client_date_time + device_type + CHECKSUM_KEY + SAVY_CHECKSUM)

con:
    CHECKSUM_KEY   = "5343"        <- Configuration.get_ChecksumKey(),  slot GOT 0x77D9368
    SAVY_CHECKSUM  = "Savvy!s0d@"  <- Configuration.get_SavyChecksum(), slot GOT 0x77EFF50

(client_date_time en formato ISO "%Y-%m-%dT%H:%M:%S" UTC, device_type como el
string de DeviceType, p.ej. "DeviceTypeAndroid".)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from .transport import PssApiError, PssHttpClient, Session

_log = logging.getLogger("pss_client.auth")

# --- Secretos del cliente (extraídos de GameAssembly.so vía IL2CPP) ---

CHECKSUM_KEY: str = "5343"
SAVY_CHECKSUM: str = "Savvy!s0d@"

# --- Device types (de pssapi.enums.DeviceType, generado desde el juego) ---

DEVICE_TYPE_ANDROID = "DeviceTypeAndroid"
DEVICE_TYPE_STEAM = "DeviceTypeSteam"
DEVICE_TYPE_GENERIC = "DeviceTypeGeneric"


EMPTY_GUID = "00000000-0000-0000-0000-000000000000"


@dataclass
class UserLogin:
    """Resultado de un login/authorize exitoso."""

    access_token: str
    user_id: int
    previous_last_login_date: Optional[str] = None
    raw: dict[str, str] = None  # type: ignore[assignment]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_pss_timestamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def create_checksum(
    device_key: str,
    client_datetime: datetime,
    device_type: str,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Genera el checksum para DeviceLogin17 (y endpoints con deviceType).

    md5(device_key + timestamp + device_type + checksum_key + savy_checksum)
    """
    if not checksum_key or not savy_checksum:
        raise ValueError("checksum_key y savy_checksum son obligatorios")
    timestamp = _to_pss_timestamp(client_datetime)
    payload = f"{device_key}{timestamp}{device_type}{checksum_key}{savy_checksum}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def create_authorize_checksum(
    email: str,
    password: str,
    client_datetime: datetime,
    checksum_key: str = CHECKSUM_KEY,
    savy_checksum: str = SAVY_CHECKSUM,
) -> str:
    """Checksum para UserEmailPasswordAuthorize (LoginExistingAccount en el juego).

    Reconstruido del desensamblado de LoginExistingAccount (RVA 0x36F9180):
    array[0]=email, array[1]=password, array[2]=timestamp, array[3]=?
    -> String.Concat(array) -> SavysodaEncryptString = md5(concat + SavyChecksum).
    La 4ª posición del array era otro captured-local; las variantes plausibles
    son (email, password, timestamp) y (email, password, timestamp, deviceKey).
    """
    timestamp = _to_pss_timestamp(client_datetime)
    payload = f"{email}{password}{timestamp}{checksum_key}{savy_checksum}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _empty_device_info() -> dict[str, Any]:
    return {
        "ClientBuild": 0,
        "ClientVersion": "",
        "DeviceName": "",
        "Locale": "",
        "OSBuild": 0,
        "OsVersion": "",
    }


def _web_device_info() -> dict[str, Any]:
    """UserDeviceInfo emulando el cliente web del juego (como pss-companion).

    Valores exactos usados por pss-companion.com (verificados funcionando
    contra la API real: DeviceLogin17 los exige desde el endurecimiento
    de 2026-09).
    """
    return {
        "OsVersion": "AD12",
        "OSBuild": "0",
        "DeviceName": "AD123",
        "Locale": "en",
        "ClientBuild": "17180",
        "ClientVersion": "0.999.43",
    }


def _parse_user_login(resp_root: ET.Element) -> UserLogin:
    # La respuesta real tiene <UserLogin> como RAÍZ del XML, no como hijo.
    # Aceptar ambas formas.
    node = resp_root if resp_root.tag == "UserLogin" else resp_root.find(".//UserLogin")
    if node is None:
        raise PssApiError("La respuesta no contiene <UserLogin>")
    token = node.attrib.get("accessToken") or ""
    user_id = int(node.attrib.get("UserId", "0"))

    # Caso cuenta Verified: errorCode 400 + email en errorMessage.
    # La API reconoce el device pero exige autenticar con el método vinculado
    # (email/password o Steam). Devolvemos el contexto para que el frontend
    # pueda redirigir al flujo correcto.
    error_code = node.attrib.get("errorCode")
    error_msg = node.attrib.get("errorMessage")
    if error_code and not token:
        user_node = node.find(".//User")
        user_email = user_node.attrib.get("Email") if user_node is not None else None
        raise PssApiError(
            json.dumps({
                "reason": "linked_account",
                "error_code": error_code,
                "message": error_msg,
                "email": user_email,
                "user_id": int(user_node.attrib.get("Id", "0")) if user_node is not None else 0,
                "hint": "Cuenta verificada: usa email+password o SteamLogin con este device_key.",
            })
        )

    if not token or not user_id:
        raise PssApiError(f"UserLogin sin token/UserId: {node.attrib}")
    return UserLogin(
        access_token=token,
        user_id=user_id,
        previous_last_login_date=node.attrib.get("PreviousLastLoginDate"),
        raw=dict(node.attrib),
    )


class PssAuth:
    """Autenticación contra la API de Pixel Starships.

    Flujos:
    - device_login: entra a la cuenta ligada a un device_key existente
      (con el device_key de tu instalación del juego -> tu cuenta).

    Único método público: email_password_login (flujo pss-companion de 3 pasos).
    """

    def __init__(
        self,
        production_server: Optional[str] = None,
        device_type: str = DEVICE_TYPE_ANDROID,
        language_key: str = "en",
    ):
        self._http = PssHttpClient(production_server)
        self.device_type = device_type
        self.language_key = language_key

    @property
    def production_server(self) -> str:
        return self._http.production_server

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "PssAuth":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    async def email_password_login(
        self,
        email: str,
        password: str,
        device_key: Optional[str] = None,
    ) -> UserLogin:
        """Login email+contraseña replicando el flujo de pss-companion.com.

        Flujo de 3 pasos (reconstruido del JS de pss-companion y verificado
        contra la API real — responde "Email or password is incorrect." con
        credenciales erróneas, es decir, checksum validado):

          1. DeviceLogin17 con deviceKey nuevo → cuenta web temporal + accessToken
             checksum1 = md5(dk + ts1 + "DeviceTypeWeb" + "5343" + "Savvy!s0d@")
          2. UserEmailPasswordAuthorize4 con el accessToken del paso 1:
             checksum2 = md5(dk + email + ts2 + accessToken1 + "5343Savvy!s0d@")
             params: clientDateTime, checksum, deviceKey, email, password,
                     accessToken(paso1), languageKey=en, isWeb=True
             → UserLogin de la cuenta REAL + refreshToken
          3. (si trae refreshToken) DeviceLogin17 con refreshToken → token owner
        """
        device_key = device_key or str(uuid4())
        _log.info("[email-password-flow] inicio email=%r device_key=%r", email, device_key)

        # PASO 1: DeviceLogin17 con deviceKey nuevo (cuenta web temporal)
        step1 = await self._device_login_raw(device_key)
        at1 = step1.attrib.get("accessToken") or ""
        _log.info("[email-password-flow] paso1 DeviceLogin17 OK user_id=%s token=%s...",
                  step1.attrib.get("UserId"), at1[:8])
        if not at1:
            raise PssApiError(f"DeviceLogin17 (paso 1) sin accessToken: {step1.attrib}")

        # PASO 2: UserEmailPasswordAuthorize4
        cdt = _to_pss_timestamp(_utc_now())
        cs2_payload = f"{device_key}{email}{cdt}{at1}{CHECKSUM_KEY}{SAVY_CHECKSUM}"
        cs2 = hashlib.md5(cs2_payload.encode("utf-8")).hexdigest()
        _log.info("[email-password-flow] paso2 Authorize4 ts=%s checksum=%s...", cdt, cs2[:8])

        resp = await self._http.request(
            "UserService/UserEmailPasswordAuthorize4",
            method="POST",
            response_root_tag="UserEmailPasswordAuthorize",
            clientDateTime=cdt,
            checksum=cs2,
            deviceKey=device_key,
            email=email,
            password=password,
            accessToken=at1,
            languageKey="en",
            isWeb="True",
        )
        node = resp.root
        if node.tag != "UserEmailPasswordAuthorize":
            node = node.find(".//UserEmailPasswordAuthorize")
        if node is None:
            _log.error("[email-password-flow] paso2 sin nodo Authorize; XML=%s", resp.raw[:500])
            raise PssApiError("Respuesta sin <UserEmailPasswordAuthorize>")
        _log.info("[email-password-flow] paso2 attrs=%s", dict(node.attrib))

        # La respuesta EXITOSA trae refreshToken + UserId (sin accessToken directo);
        # los errores traen errorMessage. Chequear éxito PRIMERO.
        refresh = node.attrib.get("refreshToken") or ""
        user_id = int(node.attrib.get("UserId", node.attrib.get("userId", "0")) or 0)

        if user_id and (refresh or node.attrib.get("accessToken")):
            # LOGIN CORRECTO: pasar al paso 3 con el refreshToken
            _log.info("[email-password-flow] paso2 LOGIN OK UserId=%s (refresh=%s)",
                      user_id, bool(refresh))
        else:
            # errores específicos de la API
            err = node.attrib.get("errorMessage") or "Authorize falló"
            _log.warning("[email-password-flow] paso2 FALLO errorMessage=%r errorCode=%r",
                         node.attrib.get("errorMessage"), node.attrib.get("errorCode"))
            if "Email or password is incorrect" in err:
                raise PssApiError("Email o contraseña incorrectos.")
            raise PssApiError(err)

        # PASO 3: si hay refreshToken, canjearlo por el token owner
        if refresh:
            try:
                step3 = await self._device_login_raw(device_key, refresh_token=refresh)
                at3 = step3.attrib.get("accessToken") or ""
                uid3 = int(step3.attrib.get("UserId", "0") or 0)
                _log.info("[email-password-flow] paso3 DeviceLogin17(refresh) UserId=%s token=%s...",
                          uid3, at3[:8])
                if at3 and uid3 == user_id:
                    return UserLogin(
                        access_token=at3,
                        user_id=uid3,
                        previous_last_login_date=step3.attrib.get("PreviousLastLoginDate"),
                        raw=dict(step3.attrib),
                    )
                _log.warning("[email-password-flow] paso3 user mismatch: %s != %s", uid3, user_id)
            except Exception as exc:
                _log.warning("[email-password-flow] paso3 excepción: %r", exc)

        # Fallback: si el paso 3 no devolvió token, usar el accessToken directo
        # del paso 2 (algunos builds lo incluyen) o el refreshToken como token.
        fallback_token = node.attrib.get("accessToken") or refresh
        return UserLogin(
            access_token=fallback_token,
            user_id=user_id,
            previous_last_login_date=node.attrib.get("PreviousLastLoginDate"),
            raw=dict(node.attrib),
        )

    async def _device_login_raw(self, device_key: str, refresh_token: str = "") -> ET.Element:
        """DeviceLogin17 crudo: devuelve el elemento <UserLogin> (sin parsear en UserLogin,
        porque en paso intermedio solo necesitamos sus atributos)."""
        cdt = _to_pss_timestamp(_utc_now())
        checksum = create_checksum(device_key, _utc_now(), self.device_type)
        body = {
            "AccessToken": EMPTY_GUID,
            "AdvertisingKey": "",
            "Checksum": checksum,
            "ClientDateTime": cdt,
            "DeviceKey": device_key,
            "DeviceType": self.device_type,
            "IsJailBroken": "false",
            "LanguageKey": self.language_key,
            "RefreshToken": refresh_token,
            "Signal": "false",
            "UserDeviceInfo": _web_device_info(),
        }
        resp = await self._http.request(
            "UserService/DeviceLogin17",
            method="POST",
            json_body=body,
            response_root_tag="UserService",
            AccessToken=body["AccessToken"],
            AdvertisingKey=body["AdvertisingKey"],
            Checksum=checksum,
            ClientDateTime=cdt,
            DeviceKey=device_key,
            DeviceType=self.device_type,
            IsJailBroken="false",
            LanguageKey=self.language_key,
            RefreshToken=refresh_token,
            Signal="false",
        )
        node = resp.root if resp.root.tag == "UserLogin" else resp.root.find(".//UserLogin")
        if node is None:
            raise PssApiError(f"DeviceLogin17 sin <UserLogin>: {resp.root.attrib}")
        if not node.attrib.get("accessToken"):
            # cuenta vinculada (errorCode 400 + email en errorMessage + <User>)
            raise PssApiError(json.dumps({
                "reason": "linked_account",
                "error_code": node.attrib.get("errorCode"),
                "message": node.attrib.get("errorMessage"),
            }))
        return node

    # ------------------------------------------------------------------ #
