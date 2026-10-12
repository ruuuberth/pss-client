"""Tests de regresión: login y sesión tras una renovación de token.

Cubre el issue #5 y el fix (d9aebeb / PR #10 f4ce0fd): el session_token que
el navegador guarda derivaba del access_token (rota cada ~1h), así que un
reinicio del server tras una renovación dejaba la sesión del navegador
huérfana → 401. Con el fix, el session_token se deriva de email+user_id y
NO cambia al renovar.

Secuencias verificadas:
  (1) login → sesión activa con token viejo
  (2) renovación del accessToken (proactiva, reactiva y /auth/refresh)
  (3) uso de la sesión renovada + reinicio del server (tokens.json persiste)

La API real de PSS se sustituye por FakePssApi en la frontera HTTP
(PssHttpClient.request): mismos XML/atributos/errores, cero red → suite
determinista en CI.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from pss_client import api as pss_api
from pss_client import auth as pss_auth
from pss_client.transport import PssApiError, PssResponse

EMAIL = "ada@example.com"
PASSWORD = "pw-ada"


# ---------------------------------------------------------------------------
# Doble de la API de PSS en la frontera HTTP
# ---------------------------------------------------------------------------

class FakePssApi:
    """Sustituto de la API real a nivel de PssHttpClient.request.

    Modela lo que la suite necesita del protocolo real:
    - UserEmailPasswordAuthorize4 con credenciales correctas → UserLogin con
      refreshToken + UserId (SIN accessToken directo), como la API real.
    - DeviceLogin17 sin RefreshToken → cuenta web temporal (paso 1 del flujo).
    - DeviceLogin17 con RefreshToken válido → accessToken NUEVO (rota en cada
      canje, como los ~1h reales).
    - DeviceLogin17 con RefreshToken desconocido → errorCode 401 sin
      accessToken (expirado/revocado).
    - GetCurrentUser → 401 "Failed to authorize access token." si el token
      está marcado como muerto (expiry simulada de ~1h).
    """

    def __init__(self) -> None:
        self.passwords: dict[str, str] = {}
        self.user_ids: dict[str, int] = {}
        # email -> refresh_tokens emitidos por login completo (log)
        self.issued: dict[str, list[str]] = {}
        # refresh_token → número de canjes servidos. Como en la API real:
        # el refreshToken NO rota (tokens.json lo reutiliza en cada
        # renovación), pero CADA canje emite un accessToken NUEVO (~1h).
        self.refresh_canjes: dict[str, int] = {}
        self.dead_access_tokens: set[str] = set()
        self.next_user_id = 7000
        self.logins_ok = 0
        self.logins_failed = 0
        self.calls: list[str] = []

    # -- helpers de modelado ---------------------------------------------------

    def register_account(self, email: str, password: str) -> None:
        self.passwords[email] = password
        self.user_ids[email] = self.next_user_id
        self.next_user_id += 1

    def expire_access_token(self, access_token: str) -> None:
        """Marca un accessToken como caducado (como a la hora real de vida)."""
        self.dead_access_tokens.add(access_token)

    def expire_all_refresh_tokens(self) -> None:
        """El server ya no acepta ningún refreshToken (revocación global)."""
        self.refresh_canjes.clear()

    def _resp(self, root: ET.Element) -> PssResponse:
        return PssResponse(root=root, raw=ET.tostring(root, encoding="unicode"))

    def _service(self, entity_tag: str, attrs: dict,
                 user_attrs: dict | None = None) -> PssResponse:
        """Respuesta con la forma real de la API: raíz <NombreServicio> y la
        entidad como hija (p.ej. <UserService><UserLogin .../></UserService>).
        user_attrs añade el hijo <User> con el perfil (Name, Email, ...),
        como en las respuestas reales de login."""
        root = ET.Element("UserService", {})
        entity = ET.SubElement(root, entity_tag, attrs)
        if user_attrs:
            ET.SubElement(entity, "User", user_attrs)
        return self._resp(root)

    # -- frontera HTTP ----------------------------------------------------------

    async def request(self, path, method="POST", json_body=None,
                      response_root_tag=None, **params):
        self.calls.append(path)
        if path == "UserService/DeviceLogin17":
            return self._device_login(params)
        if path == "UserService/UserEmailPasswordAuthorize4":
            return self._authorize4(params)
        if path == "UserService/GetCurrentUser":
            return self._get_current_user(params)
        raise AssertionError(f"endpoint inesperado en el test: {path}")

    def _device_login(self, params) -> PssResponse:
        refresh = params.get("RefreshToken") or ""
        if refresh:
            canjes = self.refresh_canjes.get(refresh)
            if canjes is None:
                # refreshToken expirado/desconocido: errorCode y SIN accessToken
                return self._service("UserLogin", {
                    "errorCode": "401",
                    "errorMessage": "Refresh token is invalid or expired.",
                })
            uid = int(refresh.split("_")[1])
            # Como en la API real: el refreshToken NO rota (se puede reusar),
            # pero CADA canje emite un accessToken distinto (~1h de vida).
            self.refresh_canjes[refresh] = canjes + 1
            return self._service("UserLogin", {
                "UserId": str(uid),
                "accessToken": f"at_{uid}_{canjes + 1}",
                "refreshToken": refresh,
            }, user_attrs={
                "Id": str(uid),
                "Name": "Ada",
                "Email": EMAIL,
            })
        # Paso 1: DeviceLogin17 con deviceKey nuevo → cuenta web temporal.
        return self._service("UserLogin", {
            "UserId": "99",
            "accessToken": f"temp_{uuid4().hex[:8]}",
        })

    def _authorize4(self, params) -> PssResponse:
        email = (params.get("email") or "").strip().lower()
        if self.passwords.get(email) != params.get("password", ""):
            self.logins_failed += 1
            # Error plano: la raíz trae errorMessage (transport.py lo eleva)
            return self._resp(ET.Element("UserEmailPasswordAuthorize",
                                         {"errorMessage": "Email or password is incorrect."}))
        # Login correcto: emitir refresh NUEVO para este login; su primer
        # canje devolverá at_{uid}_0 y los siguientes irán rotando.
        uid = self.user_ids[email]
        n = len(self.issued.setdefault(email, []))
        refresh = f"rt_{uid}_{n}"
        self.issued[email].append(refresh)
        self.refresh_canjes[refresh] = 0
        self.logins_ok += 1
        # Como la API real: Authorize4 trae refreshToken + UserId (sin
        # accessToken) + el hijo <User> con el perfil de la cuenta.
        return self._service("UserEmailPasswordAuthorize", {
            "UserId": str(uid),
            "refreshToken": refresh,
            "PreviousLastLoginDate": "2026-10-01T00:00:00",
        }, user_attrs={
            "Id": str(uid),
            "Name": email.split("@")[0].title(),
            "Email": email,
            "Credits": "1234", "Trophy": "567",
        })

    def _get_current_user(self, params) -> PssResponse:
        access = params.get("accessToken", "")
        if access in self.dead_access_tokens:
            raise PssApiError("Failed to authorize access token.")
        root = ET.Element("GetCurrentUser", {})
        ET.SubElement(root, "User", {
            "Id": "7000", "Name": "Ada", "Email": EMAIL,
            "Credits": "1234", "Trophy": "567",
        })
        return self._resp(root)


@pytest.fixture
def fake_api(monkeypatch) -> FakePssApi:
    """Parchea PssHttpClient.request para que hable con la API falsa."""
    api = FakePssApi()

    async def scripted_request(self, path, method="POST", json_body=None,
                               response_root_tag=None, **params):
        return await api.request(path, method=method, json_body=json_body,
                                 response_root_tag=response_root_tag, **params)

    monkeypatch.setattr(pss_auth.PssHttpClient, "request", scripted_request)
    return api


class FakeServer:
    """Estado del server: sesiones en memoria + tokens.json aislado en tmp."""

    def __init__(self, tokens_file):
        self.tokens_file = tokens_file
        self.sessions = pss_api.active_sessions

    def restart(self) -> int:
        """Reinicio del proceso: la memoria se pierde, tokens.json persiste."""
        pss_api.active_sessions.clear()
        return pss_api._restore_sessions()


@pytest.fixture
def server(tmp_path) -> FakeServer:
    return FakeServer(tmp_path / "tokens.json")


async def _login(email: str = EMAIL, password: str = PASSWORD) -> dict:
    return await pss_api.login(pss_api.LoginRequest(email=email, password=password))


# ---------------------------------------------------------------------------
# (1) Sesión activa con token viejo
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_login_crea_sesion_activa_con_token_viejo(fake_api, server):
    """El login deja una sesión viva bajo el session_token estable."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    assert resp["success"] is True
    st = resp["session_token"]
    assert st.startswith("sess_7000_")
    assert st in server.sessions, "la sesión debe quedar activa tras el login"
    # El access con el que se usará la sesión: el login cierra canjeando el
    # refresh (paso 3) → primera generación post-login (no la temp del paso 1).
    assert server.sessions[st].access_token == "at_7000_1"
    # Credenciales persistidas para poder renovar tras un reinicio.
    entry = pss_auth._load_tokens()[EMAIL]
    assert entry["user_id"] == 7000
    assert entry["refresh_token"] == "rt_7000_0"
    assert entry["device_key"]


# ---------------------------------------------------------------------------
# (2) Renovación del token → (3) uso de la sesión renovada
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_renovacion_no_cambia_el_session_token(fake_api, server):
    """Núcleo del issue #5: renovar el accessToken NO cambia el session_token."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]
    access_viejo = resp["access_token"]
    assert access_viejo == "at_7000_1"

    ok = await pss_api._refresh_session_token(st)
    assert ok is True
    # El session_token (lo que el navegador guardó) sigue siendo la llave…
    assert st in server.sessions, "la sesión debe seguir viva bajo el MISMO session_token"
    # …pero el accessToken interno SÍ rotó (para eso existe la renovación).
    assert server.sessions[st].access_token == "at_7000_2"
    assert server.sessions[st].access_token != access_viejo
    # La sesión renovada se usa sin re-login:
    me = await pss_api.user_me(pss_api.SessionRequest(session_token=st))
    assert me["success"] is True
    assert me["user"]["user_id"] == "7000"


@pytest.mark.anyio
async def test_session_token_derivado_de_email_no_del_access(fake_api, server):
    """Formato estable sess_{uid}_{uuid5(email)}; el formato viejo no vuelve."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    uid = resp["user_id"]
    esperado = pss_auth._session_token_for(EMAIL, uid)
    assert resp["session_token"] == esperado
    # REGRESIÓN del formato viejo sess_{uid}_{access_token[:16]}:
    assert resp["access_token"][:16] not in resp["session_token"]
    # Normalización documentada (strip + lower) → mismo session_token.
    assert pss_auth._session_token_for(f"  {EMAIL.upper()} ", uid) == esperado


@pytest.mark.anyio
async def test_login_con_email_en_mayusculas_misma_sesion(fake_api, server):
    """Login con mayúsculas/espacios → mismo session_token que en minúsculas."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login(email=f"  {EMAIL.upper()} ")
    st = resp["session_token"]
    assert st == pss_auth._session_token_for(EMAIL, resp["user_id"])
    # Y la sesión con mayúsculas es renovable (lookup normalizado del refresh):
    assert await pss_api._refresh_session_token(st) is True


# ---------------------------------------------------------------------------
# Secuencia EXACTA del issue #5: renovación + reinicio del server
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_reinicio_del_server_tras_renovacion(fake_api, server):
    """login → renovación → reinicio → el session_token del navegador sigue vivo."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st_navegador = resp["session_token"]

    # Renovación (la proactiva del heartbeat a los 45 min, p.ej.)
    assert await pss_api._refresh_session_token(st_navegador) is True

    # El server se reinicia: la memoria se pierde, tokens.json persiste.
    assert server.restart() == 1

    # El navegador reintenta con el MISMO session_token → sesión viva y con
    # el accessToken YA RENOVADO restaurado desde tokens.json.
    login = server.sessions.get(st_navegador)
    assert login is not None, (
        "REGRESIÓN issue #5: el session_token del navegador quedó huérfano "
        "tras renovar + reiniciar (401 obligando a re-login)"
    )
    assert login.access_token == "at_7000_2"
    me = await pss_api.user_me(pss_api.SessionRequest(session_token=st_navegador))
    assert me["success"] is True


@pytest.mark.anyio
async def test_dos_renovaciones_y_reinicio(fake_api, server):
    """Varias renovaciones seguidas + reinicio: el session_token no cambia."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]

    assert await pss_api._refresh_session_token(st) is True
    assert await pss_api._refresh_session_token(st) is True
    assert server.sessions[st].access_token == "at_7000_3"

    server.restart()
    login = server.sessions.get(st)
    assert login is not None
    assert login.access_token == "at_7000_3"
    me = await pss_api.user_me(pss_api.SessionRequest(session_token=st))
    assert me["user"]["user_id"] == "7000"


@pytest.mark.anyio
async def test_restore_sessions_es_idempotente(fake_api, server):
    """Restaurar dos veces no duplica sesiones ni cambia el session_token."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]
    assert server.restart() == 1
    assert server.restart() == 1
    assert list(server.sessions) == [st]


@pytest.mark.anyio
async def test_sin_renovacion_previa_el_reinicio_tambien_mantiene_sesion(fake_api, server):
    """Reinicio SIN renovación previa: sesión restaurada con el token guardado."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]
    server.restart()
    login = server.sessions.get(st)
    assert login is not None
    # tokens.json guarda el access del paso 3 (primera generación post-login)
    assert login.access_token == "at_7000_1"


# ---------------------------------------------------------------------------
# Casos límite: token expirado / inválido post-renovación
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_refresh_con_session_token_desconocido(fake_api, server):
    """session_token que no existe → False; el caller debe re-loguear."""
    assert await pss_api._refresh_session_token("sess_nadie_inexistente") is False


@pytest.mark.anyio
async def test_refresh_sin_refresh_token_persistido(fake_api, server):
    """Sesión viva pero tokens.json perdido → renovación False; sesión en
    memoria NO se borra por fallar la renovación."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]
    server.tokens_file.unlink()  # p.ej. contenedor efímero sin volumen
    assert await pss_api._refresh_session_token(st) is False
    assert st in server.sessions


@pytest.mark.anyio
async def test_refresh_con_refresh_token_expirado(fake_api, server):
    """refreshToken revocado/expirado post-reinicio → renovación False y el
    endpoint autenticado responde 401 estructurado (reason=session_expired)."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]

    # El accessToken viejo caducó y el refreshToken ya no sirve:
    fake_api.expire_access_token(resp["access_token"])
    fake_api.expire_all_refresh_tokens()
    # Sesión restaurada tras reinicio (con el access caducado):
    server.restart()

    assert await pss_api._refresh_session_token(st) is False
    with pytest.raises(pss_api.HTTPException) as excinfo:
        pss_api._require_session("sess_token_que_no_existe")
    assert excinfo.value.status_code == 401
    assert excinfo.value.detail["reason"] == "session_expired"


@pytest.mark.anyio
async def test_access_token_expirado_se_renueva_reactivamente_en_user_me(fake_api, server):
    """/user/me con accessToken caducado (~1h) → renueva con el refresh y reintenta."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]
    fake_api.expire_access_token(resp["access_token"])

    me = await pss_api.user_me(pss_api.SessionRequest(session_token=st))
    assert me["success"] is True
    assert me["user"]["name"] == "Ada"
    # La renovación reactiva dejó la sesión con el token nuevo:
    assert server.sessions[st].access_token == "at_7000_2"
    assert fake_api.calls.count("UserService/GetCurrentUser") == 2  # fallo + reintento


@pytest.mark.anyio
async def test_user_me_sin_renovacion_posible_devuelve_datos_locales(fake_api, server):
    """Si ni el access ni el refresh sirven, /user/me cae al perfil local
    (raw del login) en vez de romper: el frontend decide si re-loguea."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]
    fake_api.expire_access_token(resp["access_token"])
    fake_api.expire_all_refresh_tokens()

    me = await pss_api.user_me(pss_api.SessionRequest(session_token=st))
    assert me["success"] is True
    assert me["user"]["user_id"] == resp["user_id"]
    assert me["user"]["name"] == "Ada"  # del raw del login, no de la API


@pytest.mark.anyio
async def test_auth_refresh_endpoint_devuelve_renewed(fake_api, server):
    """/auth/refresh: renewed=True con refresh válido, False si no hay sesión."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]

    out = await pss_api.auth_refresh(pss_api.SessionRequest(session_token=st))
    assert out == {"success": True, "renewed": True}

    out = await pss_api.auth_refresh(
        pss_api.SessionRequest(session_token="sess_nadie"))
    assert out == {"success": True, "renewed": False}


@pytest.mark.anyio
async def test_login_rechaza_credenciales_incorrectas(fake_api, server):
    """Contraseña errónea → 401; sin sesión y sin tokens persistidos."""
    fake_api.register_account(EMAIL, PASSWORD)
    with pytest.raises(pss_api.HTTPException) as excinfo:
        await _login(password="pw-equivocada")
    assert excinfo.value.status_code == 401
    assert "incorrectos" in str(excinfo.value.detail)
    assert server.sessions == {}
    assert pss_auth._load_tokens() == {}


@pytest.mark.anyio
async def test_login_rechaza_email_desconocido(fake_api, server):
    """Email no registrado → 401 (la API falsa lo trata como credencial mala)."""
    with pytest.raises(pss_api.HTTPException) as excinfo:
        await _login(email="fantasma@example.com", password="x")
    assert excinfo.value.status_code == 401
    assert server.sessions == {}


@pytest.mark.anyio
async def test_logout_borra_la_sesion(fake_api, server):
    """Tras /auth/logout el session_token ya no autentica (401 estructurado)."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]
    out = await pss_api.logout(pss_api.SessionRequest(session_token=st))
    assert out == {"success": True}
    with pytest.raises(pss_api.HTTPException) as excinfo:
        pss_api._require_session(st)
    assert excinfo.value.status_code == 401


# ---------------------------------------------------------------------------
# Renovación proactiva por edad del accessToken (issue #3 sobre secuencia #5)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_renovacion_proactiva_a_los_45_minutos(fake_api, server):
    """saved_at con edad ≥ 45 min → _access_token_age_seconds dispara la
    renovación ANTES de que el heartbeat vea un token muerto."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]

    # El token fue emitido hace 46 min: reescribir saved_at como lo haría el
    # tiempo real (saved_at = momento de emisión del access persistido).
    emitido = datetime.now(timezone.utc) - timedelta(minutes=46)
    entry = pss_auth._load_tokens()[EMAIL]
    entry["saved_at"] = emitido.strftime("%Y-%m-%dT%H:%M:%S")
    with open(server.tokens_file, "w") as f:
        json.dump({EMAIL: entry}, f)

    age = pss_api._access_token_age_seconds(server.sessions[st])
    assert age is not None and age >= pss_api.PROACTIVE_REFRESH_AFTER

    assert await pss_api._refresh_session_token(st) is True
    assert server.sessions[st].access_token == "at_7000_2"


@pytest.mark.anyio
async def test_saved_at_corrupto_no_renueva_proactivamente(fake_api, server):
    """saved_at ilegible → edad None → la vía proactiva no dispara (queda la
    reactiva: fallar → renovar → reintentar)."""
    fake_api.register_account(EMAIL, PASSWORD)
    resp = await _login()
    st = resp["session_token"]
    entry = pss_auth._load_tokens()[EMAIL]
    entry["saved_at"] = "no-es-una-fecha"
    with open(server.tokens_file, "w") as f:
        json.dump({EMAIL: entry}, f)
    assert pss_api._access_token_age_seconds(server.sessions[st]) is None
    # La renovación directa sigue funcionando (el refresh no dependió de ello):
    assert await pss_api._refresh_session_token(st) is True
