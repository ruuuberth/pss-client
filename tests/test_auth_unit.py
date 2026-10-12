"""Tests unitarios puros de pss_client (sin red, sin ficheros de usuario).

Cubren las partes deterministas del cliente: checksum de DeviceLogin17,
construcción del request, flujo email+password de 3 pasos y la renovación de
accessToken — con el transporte HTTP (PssHttpClient) sustituido por un fake.
"""

import asyncio
import hashlib
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

import pss_client.auth as auth_module
from pss_client.auth import (
    CHECKSUM_KEY,
    DEVICE_TYPE_ANDROID,
    SAVY_CHECKSUM,
    PssAuth,
    UserLogin,
    _session_token_for,
    _to_pss_timestamp,
    _web_device_info,
    create_checksum,
)
from pss_client.transport import PssApiError, PssResponse

FIXED_DT = datetime(2026, 10, 12, 1, 2, 3, tzinfo=timezone.utc)
FIXED_TS = "2026-10-12T01:02:03"


def _md5(payload: str) -> str:
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def _checksum_reference(device_key: str, timestamp: str, device_type: str) -> str:
    """Fórmula documentada: md5(dk + timestamp + deviceType + key + savy)."""
    return _md5(f"{device_key}{timestamp}{device_type}{CHECKSUM_KEY}{SAVY_CHECKSUM}")


class FakeHttp:
    """Sustituto de PssHttpClient: graba las llamadas y responde XML fijo."""

    def __init__(self, responses=None):
        self.calls = []
        self._responses = responses or {}

    async def request(self, path, method="POST", json_body=None,
                      response_root_tag=None, **params):
        self.calls.append({
            "path": path, "method": method, "params": dict(params),
            "json_body": json_body, "response_root_tag": response_root_tag,
        })
        xml = self._responses.get(path, '<Error errorMessage="sin stub"/>')
        return PssResponse(root=ET.fromstring(xml), raw=xml)

    def calls_to(self, path):
        return [c for c in self.calls if c["path"] == path]


@pytest.fixture
def fake_tokens_file(monkeypatch, tmp_path):
    """TOKENS_FILE apuntando a tmp: ningún test toca ~/.pss-client."""
    path = tmp_path / "tokens.json"
    monkeypatch.setattr(auth_module, "TOKENS_FILE", str(path))
    return path


@pytest.fixture
def fixed_now(monkeypatch):
    monkeypatch.setattr(auth_module, "_utc_now", lambda: FIXED_DT)


# --- create_checksum ---------------------------------------------------------


def test_create_checksum_longitud_md5():
    checksum = create_checksum(str(uuid4()), FIXED_DT, DEVICE_TYPE_ANDROID)
    assert len(checksum) == 32
    int(checksum, 16)  # hexdigest válido


def test_create_checksum_formula_exacta():
    expected = _checksum_reference("device-key-123", FIXED_TS, DEVICE_TYPE_ANDROID)
    assert create_checksum("device-key-123", FIXED_DT, DEVICE_TYPE_ANDROID) == expected


def test_create_checksum_claves_vacias_rechazadas():
    with pytest.raises(ValueError):
        create_checksum("dk", FIXED_DT, DEVICE_TYPE_ANDROID, checksum_key="")
    with pytest.raises(ValueError):
        create_checksum("dk", FIXED_DT, DEVICE_TYPE_ANDROID, savy_checksum="")


# --- helpers de módulo -------------------------------------------------------


def test_to_pss_timestamp_formato():
    assert _to_pss_timestamp(FIXED_DT) == FIXED_TS


def test_session_token_estable_y_derivado_de_cuenta():
    expected = f"sess_42_{uuid5(NAMESPACE_URL, 'pss-client:session:mail@example.com')}"
    assert _session_token_for("Mail@Example.com", 42) == expected
    # Normalización: mayúsculas/espacios del email no cambian el token.
    assert _session_token_for("  mail@example.com ", 42) == expected
    # user_id distinto → token distinto.
    assert _session_token_for("mail@example.com", 43) != expected


def test_web_device_info_emula_cliente_web():
    info = _web_device_info()
    assert info == {
        "OsVersion": "AD12",
        "OSBuild": "0",
        "DeviceName": "AD123",
        "Locale": "en",
        "ClientBuild": "17180",
        "ClientVersion": "0.999.43",
    }


# --- PssAuth._device_login_raw (request sin red) -----------------------------


def test_device_login_raw_construye_checksum_y_params(fixed_now):
    auth = PssAuth()
    auth._http = FakeHttp({
        "UserService/DeviceLogin17":
            '<UserService><UserLogin UserId="1" accessToken="tok"/></UserService>',
    })

    node = asyncio.run(auth._device_login_raw("dk-1", refresh_token="ref-9"))

    call = auth._http.calls_to("UserService/DeviceLogin17")[0]
    assert call["method"] == "POST"
    assert call["response_root_tag"] == "UserService"
    # El checksum del request encaja con la fórmula y con el ClientDateTime enviado.
    expected = _checksum_reference("dk-1", call["params"]["ClientDateTime"],
                                   DEVICE_TYPE_ANDROID)
    assert call["params"]["Checksum"] == expected
    assert call["params"]["DeviceKey"] == "dk-1"
    assert call["params"]["RefreshToken"] == "ref-9"
    assert call["json_body"]["Checksum"] == expected
    assert call["json_body"]["UserDeviceInfo"] == _web_device_info()
    # El nodo devuelto es el <UserLogin> de la respuesta.
    assert node.attrib["accessToken"] == "tok"


def test_device_login_raw_sin_access_token_lanza_linked_account():
    auth = PssAuth()
    auth._http = FakeHttp({
        "UserService/DeviceLogin17":
            '<UserService><UserLogin errorCode="400" errorMessage="user@example.com"/>'
            "<User/></UserService>",
    })

    with pytest.raises(PssApiError) as excinfo:
        asyncio.run(auth._device_login_raw("dk-1"))
    payload = json.loads(str(excinfo.value))
    assert payload["reason"] == "linked_account"


# --- refresh_access_token ----------------------------------------------------


def test_refresh_access_token_ok():
    auth = PssAuth()
    auth._http = FakeHttp({
        "UserService/DeviceLogin17":
            '<UserService><UserLogin UserId="42" accessToken="token-nuevo"/></UserService>',
    })

    login = asyncio.run(auth.refresh_access_token("dk-1", "ref-1"))

    assert isinstance(login, UserLogin)
    assert login.access_token == "token-nuevo"
    assert login.user_id == 42


def test_refresh_access_token_devuelve_none_si_no_hay_user_id():
    auth = PssAuth()
    auth._http = FakeHttp({
        "UserService/DeviceLogin17":
            '<UserService><UserLogin UserId="0" accessToken="token-huerfano"/></UserService>',
    })

    assert asyncio.run(auth.refresh_access_token("dk-1", "ref-1")) is None


# --- email_password_login (flujo de 3 pasos, offline) ------------------------


def _fake_http_flujo_completo():
    return FakeHttp({
        # Paso 1: DeviceLogin17 con deviceKey nuevo → cuenta web temporal.
        # Paso 3: mismo endpoint pero con RefreshToken en el body → token owner.
        "UserService/DeviceLogin17":
            '<UserService><UserLogin UserId="111" accessToken="token-temporal"/></UserService>',
        # Paso 2: Authorize4 de la cuenta real (trae refreshToken, sin accessToken).
        "UserService/UserEmailPasswordAuthorize4":
            '<UserEmailPasswordAuthorize UserId="42" refreshToken="refresh-xyz" '
            'PreviousLastLoginDate="2026-01-01T00:00:00"/>',
    })


def test_email_password_login_flujo_completo(fake_tokens_file, fixed_now):
    email = "user@example.com"
    auth = PssAuth()
    auth._http = _fake_http_flujo_completo()

    login = asyncio.run(auth.email_password_login(email, "secreto"))

    # El paso 3 (canje del refresh) exige que el UserId devuelto coincida con
    # el del paso 2; este stub siempre responde UserId=111, así que el flujo
    # cae al fallback documentado: accessToken = refreshToken del paso 2.
    assert login.user_id == 42
    assert login.access_token == "refresh-xyz"
    assert login.previous_last_login_date == "2026-01-01T00:00:00"

    # El Authorize4 (paso 2) lleva el checksum md5(dk + email + ts + at1 + keys).
    a4 = auth._http.calls_to("UserService/UserEmailPasswordAuthorize4")[0]
    dk = str(uuid5(NAMESPACE_URL, f"pss-client:{email}"))
    assert a4["params"]["deviceKey"] == dk
    expected = _md5(f"{dk}{email}{a4['params']['clientDateTime']}"
                    f"{a4['params']['accessToken']}{CHECKSUM_KEY}{SAVY_CHECKSUM}")
    assert a4["params"]["checksum"] == expected
    assert a4["params"]["password"] == "secreto"
    assert a4["params"]["isWeb"] == "True"

    # El refreshToken de la cuenta real queda persistido para el próximo login.
    saved = json.loads(fake_tokens_file.read_text())
    assert saved[email]["refresh_token"] == "refresh-xyz"
    assert saved[email]["user_id"] == 42


def test_email_password_login_credenciales_incorrectas(fake_tokens_file, fixed_now):
    auth = PssAuth()
    auth._http = FakeHttp({
        "UserService/DeviceLogin17":
            '<UserService><UserLogin UserId="111" accessToken="token-temporal"/></UserService>',
        "UserService/UserEmailPasswordAuthorize4":
            '<UserEmailPasswordAuthorize errorMessage="Email or password is incorrect." '
            'errorCode="401"/>',
    })

    with pytest.raises(PssApiError, match="contrase"):
        asyncio.run(auth.email_password_login("user@example.com", "mal"))
    # Sin login correcto no se persiste nada.
    assert not fake_tokens_file.exists()
