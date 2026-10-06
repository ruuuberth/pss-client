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
from uuid import NAMESPACE_URL, uuid4, uuid5

from .transport import PssApiError, PssHttpClient

_log = logging.getLogger("pss_client.auth")

# --- Secretos del cliente (extraídos de GameAssembly.so vía IL2CPP) ---
# Prioridad: variables de entorno (GitHub Secrets / .env) → fallback embebido.

CHECKSUM_KEY: str = os.environ.get("PSS_CHECKSUM_KEY", "5343")
SAVY_CHECKSUM: str = os.environ.get("PSS_SAVY_CHECKSUM", "Savvy!s0d@")

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


TOKENS_FILE = os.path.expanduser("~/.pss-client/tokens.json")


def _load_tokens() -> dict[str, dict]:
    """Tokens persistidos por email (como el juego persiste su sesión)."""
    try:
        with open(TOKENS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_token(email: str, device_key: str, refresh_token: str,
                user_id: int, access_token: str = "") -> None:
    """Persiste el refreshToken de una cuenta (supervive reinicios del server).

    El refreshToken es el equivalente a la sesión del dispositivo en el
    juego real: mientras sea válido, el login no necesita crear otra cuenta
    temporal (evita errorCode 445) y funciona aunque el deviceKey ya esté
    vinculado (evita el 400 linked_account).

    El accessToken se guarda además para que la API REST pueda RESTAURAR la
    sesión al arrancar (el navegador no necesita re-loguear tras un reinicio).
    """
    email = email.strip().lower()
    tokens = _load_tokens()
    entry = {
        "device_key": device_key,
        "refresh_token": refresh_token,
        "user_id": user_id,
        "saved_at": _to_pss_timestamp(_utc_now()),
    }
    if access_token:
        entry["access_token"] = access_token
    tokens[email] = entry
    os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)
    _log.info("[tokens] refreshToken persistido para %s", email)


def _session_token_for(email: str, user_id: int) -> str:
    """session_token ESTABLE por cuenta (no rota con las renovaciones).

    Antes derivaba del access_token (rota cada ~1h), así que cualquier
    reinicio del server tras una renovación dejaba el session_token del
    navegador huérfano → 401. Derivarlo del email+user_id lo hace estable
    para siempre.
    """
    return f"sess_{user_id}_{uuid5(NAMESPACE_URL, f'pss-client:session:{email.strip().lower()}')}"


def _email_for_user(user_id: int) -> Optional[str]:
    """Email cuyo token persistido corresponde a user_id (para renovar sesiones)."""
    for email, entry in _load_tokens().items():
        if int(entry.get("user_id") or 0) == user_id:
            return email
    return None


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
        if not device_key:
            # deviceKey DETERMINISTA por email: mismo email → mismo "dispositivo"
            # → el paso 1 reutiliza la cuenta web temporal ya creada en vez de
            # crear una nueva por cada login (error 445: "too many accounts").
            device_key = str(uuid5(NAMESPACE_URL, f"pss-client:{email}"))
        _log.info("[email-password-flow] inicio email=%r device_key=%r", email, device_key)

        # PASO 0: refreshToken persistido de un login anterior → login directo.
        # Es lo que hace el juego real: la sesión del dispositivo se guarda y
        # no se vuelve a pedir email/password. Si el refresh falla (expirado/
        # revocado) se cae al flujo completo de abajo.
        saved = _load_tokens().get(email.strip().lower())
        if saved and saved.get("refresh_token"):
            try:
                _log.info("[email-password-flow] paso0 probando refreshToken persistido…")
                step0 = await self._device_login_raw(
                    saved.get("device_key") or device_key,
                    refresh_token=saved["refresh_token"])
                at0 = step0.attrib.get("accessToken") or ""
                uid0 = int(step0.attrib.get("UserId", "0") or 0)
                if at0 and uid0:
                    _log.info("[email-password-flow] paso0 LOGIN DIRECTO UserId=%s (refresh persistido)", uid0)
                    _save_token(email, saved.get("device_key") or device_key,
                                saved["refresh_token"], uid0, access_token=at0)
                    return UserLogin(
                        access_token=at0,
                        user_id=uid0,
                        previous_last_login_date=step0.attrib.get("PreviousLastLoginDate"),
                        raw=dict(step0.attrib),
                    )
            except Exception as exc:
                _log.info("[email-password-flow] paso0 refresh no válido (%r) — flujo completo", exc)

        # PASO 1: DeviceLogin17 con deviceKey nuevo (cuenta web temporal).
        # Si el deviceKey determinista ya quedó VINCULADO a la cuenta del
        # email (login previo cuyo refresh no se persistió), la API responde
        # errorCode=400 con el email en errorMessage. Sin refresh guardado no
        # hay tokens en esa respuesta → hay que rotar el deviceKey y reintentar
        # (una sola vez; la rotación es determinista por email+intentos).
        step1 = None
        for attempt in range(2):
            try:
                step1 = await self._device_login_raw(device_key)
                break
            except PssApiError as exc:
                msg = str(exc)
                if "linked_account" in msg and attempt == 0:
                    _log.warning("[email-password-flow] paso1 deviceKey ya vinculado — rotando")
                    device_key = str(uuid5(NAMESPACE_URL, f"pss-client:{email}#{attempt + 1}"))
                    continue
                raise
        if step1 is None:
            raise PssApiError("DeviceLogin17 (paso 1) falló tras rotar deviceKey")
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
            if refresh:
                # Persistir el refresh AHORA (antes del paso 3) para que el
                # próximo login pueda entrar directo aunque el server se
                # reinicie o este proceso muera. El deviceKey que usamos es
                # el definitivo (incluida la rotación del paso 1).
                _save_token(email, device_key, refresh, user_id)
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
                    _save_token(email, device_key, refresh, user_id, access_token=at3)
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
        if refresh:
            _save_token(email, device_key, refresh, user_id, access_token=fallback_token)
        return UserLogin(
            access_token=fallback_token,
            user_id=user_id,
            previous_last_login_date=node.attrib.get("PreviousLastLoginDate"),
            raw=dict(node.attrib),
        )

    async def refresh_access_token(self, device_key: str, refresh_token: str) -> Optional[UserLogin]:
        """Renueva el accessToken con el refreshToken (los accessToken caducan ~1h).

        Es el mismo canje del paso 3 del login (DeviceLogin17 con RefreshToken).
        El juego real lo hace en segundo plano: cuando el accessToken expira,
        pide otro con el RefreshToken de larga duración. Devuelve None si el
        refresh ya no sirve (toca login completo con email+password).
        """
        node = await self._device_login_raw(device_key, refresh_token=refresh_token)
        at = node.attrib.get("accessToken") or ""
        uid = int(node.attrib.get("UserId", "0") or 0)
        if not at or not uid:
            return None
        return UserLogin(
            access_token=at,
            user_id=uid,
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
        # Mezclar los atributos del hijo <User> (Name, Email, Credits, Trophy...)
        # en el propio nodo, para que raw=dict(node.attrib) del caller los vea.
        user_child = node.find(".//User")
        if user_child is not None:
            for k, v in user_child.attrib.items():
                node.attrib.setdefault(k, v)
        return node

    # ------------------------------------------------------------------ #
