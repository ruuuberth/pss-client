"""CLI de pss_client — subcomandos de línea de comandos contra la API PSS.

Primer subcomando: `whoami`. Muestra el email y el estado de la sesión
persistida en `~/.pss-client/tokens.json`, verificándola en vivo contra
`UserService/GetCurrentUser` — el mismo endpoint que expone el gateway en
`POST /user/me`. Reutiliza la infraestructura existente (`_load_tokens`,
`PssHttpClient`): no hay ningún mecanismo de auth nuevo.

Uso:
    uv run python -m pss_client.cli whoami
    pss-client whoami            # console script, tras `uv sync`

Códigos de salida:
    0   sesión activa
    1   error inesperado (red caída, respuesta corrupta de la API…)
    2   sin sesión guardada (o --email desconocido, o llamada sin subcomando)
    3   sesión expirada (la API rechazó el accessToken)

`whoami` es de SOLO LECTURA: nunca renueva tokens ni escribe tokens.json
(la renovación es cosa del gateway; ver api.py).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import traceback
from typing import Any, Mapping, Optional

import aiohttp

from .auth import _load_tokens
from .transport import DEFAULT_PRODUCTION_SERVER, PssApiError, PssHttpClient, PssResponse

_log = logging.getLogger("pss_client.cli")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_AUTHENTICATED = 2
EXIT_SESSION_EXPIRED = 3

# El único flujo de login existente es el gateway + dashboard web:
LOGIN_HINT = (
    "No autenticado. Inicia sesión primero: ejecuta el gateway con "
    "`uv run python -m pss_client.api` y entra con tu email y contraseña "
    "en http://localhost:8000."
)

# Marcador del accessToken caducado/rechazado — el MISMO que usa el gateway
# (api.py: /user/me, /ship/info…) para detectar token muerto y renovar.
_AUTH_FAILURE_MARKER = "authorize access token"


def _pick_entry(
    tokens: Mapping[str, dict],
    email: Optional[str] = None,
) -> tuple[Optional[str], Optional[dict]]:
    """Elige (email, entry) de tokens.json.

    Con --email: la cuenta pedida (las claves se guardan en minúsculas,
    ver auth._save_token); lanza KeyError si no existe.
    Sin --email: la cuenta más reciente por `saved_at` (formato ISO, orden
    lexicográfico). Si no hay tokens devuelve (None, None).
    """
    if email:
        key = email.strip().lower()
        if key in tokens:
            return key, tokens[key]
        exact = next((k for k in tokens if k.lower() == key), None)
        if exact is not None:
            return exact, tokens[exact]
        raise KeyError(email)
    if not tokens:
        return None, None
    latest = max(tokens, key=lambda k: tokens[k].get("saved_at") or "")
    return latest, tokens[latest]


async def _fetch_current_user(http: Any, access_token: str) -> dict[str, str]:
    """GET UserService/GetCurrentUser — mismo patrón que api.py (/user/me)."""
    resp: PssResponse = await http.request(
        "UserService/GetCurrentUser",
        method="GET",
        response_root_tag="GetCurrentUser",
        accessToken=access_token,
    )
    node = resp.root if resp.root.tag == "GetCurrentUser" else resp.root.find(".//GetCurrentUser")
    if node is None:
        return {}
    user_node = node.find(".//User")
    return dict(user_node.attrib) if user_node is not None else dict(node.attrib)


def _render(result: dict, as_json: bool = False) -> None:
    """Imprime el resultado de whoami en texto plano o JSON (stdout)."""
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return
    print(f"Email:  {result.get('email') or '(desconocido)'}")
    name, user_id = result.get("name"), result.get("user_id")
    if name and user_id:
        print(f"Usuario: {name} (Id {user_id})")
    elif user_id:
        print(f"UserId: {user_id}")
    if result.get("status") == "active":
        print("Sesión: activa (accessToken verificado contra la API)")
    else:
        print("Sesión: expirada")
        if result.get("reason"):
            print(f"Motivo: {result['reason']}")
        print(LOGIN_HINT)


async def _cmd_whoami(args: argparse.Namespace, http: Optional[Any] = None) -> int:
    """Subcomando whoami: email + estado de la sesión persistida.

    `http` es inyectable para los tests (FakeHttpClient); en producción se
    crea un PssHttpClient real (el mismo cliente del gateway).
    """
    tokens = _load_tokens()
    try:
        email, entry = _pick_entry(tokens, getattr(args, "email", None))
    except KeyError:
        available = ", ".join(sorted(tokens)) or "(ninguna)"
        print(f"No hay sesión guardada para {args.email!r}. Cuentas disponibles: {available}")
        return EXIT_NOT_AUTHENTICATED
    if not entry:
        print(LOGIN_HINT)
        return EXIT_NOT_AUTHENTICATED

    access_token = entry.get("access_token") or ""
    user_id = entry.get("user_id") or None
    as_json = bool(getattr(args, "json", False))

    # Solo refreshToken guardado (p. ej. tokens.json de una versión vieja):
    # sin accessToken no hay sesión verificable → se reporta expirada.
    if not access_token:
        _render({
            "email": email,
            "status": "expired",
            "user_id": user_id,
            "reason": "no hay accessToken persistido (solo refreshToken)",
        }, as_json=as_json)
        return EXIT_SESSION_EXPIRED

    own_http = http is None
    if own_http:
        http = PssHttpClient()
    try:
        try:
            user = await _fetch_current_user(http, access_token)
        except PssApiError as exc:
            if _AUTH_FAILURE_MARKER in str(exc).lower():
                _render({
                    "email": email,
                    "status": "expired",
                    "user_id": user_id,
                    "reason": f"la API rechazó el accessToken ({exc})",
                }, as_json=as_json)
                return EXIT_SESSION_EXPIRED
            print(f"Error de la API de PSS: {exc}")
            return EXIT_ERROR
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            print(f"Error de red conectando con {DEFAULT_PRODUCTION_SERVER}: {exc}")
            return EXIT_ERROR
    finally:
        if own_http:
            await http.close()

    # El email de la API es el autoritativo; si viene vacío, el persistido.
    api_email = (user.get("Email") or "").strip() or email
    _render({
        "email": api_email,
        "status": "active",
        "user_id": user.get("Id") or user.get("UserId") or user_id,
        "name": user.get("Name") or None,
    }, as_json=as_json)
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    """Parser con el registro de subcomandos (patrón para los que vengan)."""
    parser = argparse.ArgumentParser(
        prog="pss-client",
        description=(
            "CLI de pss_client contra la API de Pixel Starships "
            "(usa la sesión persistida en ~/.pss-client/tokens.json)."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMANDO")

    whoami = sub.add_parser(
        "whoami",
        help="muestra el email y el estado de la sesión actual",
        description=(
            "Verifica contra la API (UserService/GetCurrentUser) la sesión "
            "persistida y muestra el email del usuario y si sigue activa o "
            "ha expirado. Es de solo lectura: no renueva tokens."
        ),
    )
    whoami.add_argument(
        "--email",
        help="cuenta concreta cuando hay varias sesiones guardadas "
             "(por defecto: la más reciente por saved_at)",
    )
    whoami.add_argument(
        "--json", action="store_true", help="salida en JSON (para scripts)")
    whoami.add_argument(
        "--debug", action="store_true",
        help="logs detallados de cada request (nivel DEBUG)")
    whoami.set_defaults(func=_cmd_whoami)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return EXIT_NOT_AUTHENTICATED
    if getattr(args, "debug", False):
        logging.basicConfig(
            level=logging.DEBUG, format="%(levelname)-7s [%(name)s] %(message)s")
    else:
        # salida limpia: whoami ya explica el motivo (p. ej. token rechazado);
        # los logs de transport/auth solo con --debug
        logging.getLogger("pss_client").setLevel(logging.ERROR)
    try:
        return asyncio.run(func(args))
    except KeyboardInterrupt:
        print("Interrumpido.", file=sys.stderr)
        return 130
    except Exception as exc:  # nunca un stack trace crudo al usuario
        if getattr(args, "debug", False):
            traceback.print_exc()
        print(f"Error inesperado: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
