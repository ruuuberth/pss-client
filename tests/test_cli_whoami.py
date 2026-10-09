"""Tests unitarios del subcomando `whoami` (pss_client.cli).

Cubren los tres escenarios de aceptación con la API mockeada:
1. sesión válida  → email + 'sesión activa', exit 0
2. sin credenciales → mensaje claro con pista de login, exit != 0
3. token expirado → estado 'expirada' sin crash, exit != 0

Todo pasa por `cli.main()` (parser incluido) para validar también los
códigos de salida reales del entrypoint. La API se mockea inyectando un
PssHttpClient falso en pss_client.cli — whoami la usa solo de LECTURA.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import aiohttp
import pytest

import pss_client.cli as cli_mod
from pss_client.auth import TOKENS_FILE
from pss_client.transport import PssApiError, PssResponse

# --- Fakes ------------------------------------------------------------------ #


class FakeHttpClient:
    """PssHttpClient de mentira: devuelve XML fijo o lanza una excepción."""

    def __init__(self, response_xml: str | None = None, exc: Exception | None = None):
        self.response_xml = response_xml
        self.exc = exc
        self.calls: list[tuple] = []
        self.closed = False

    async def request(self, path, method="POST", response_root_tag=None, **params):
        self.calls.append((path, method, params))
        if self.exc is not None:
            raise self.exc
        return PssResponse(root=ET.fromstring(self.response_xml), raw=self.response_xml)

    async def close(self):
        self.closed = True


def _user_xml(email: str = "alice@example.com", name: str = "Alice",
              user_id: str = "42", with_email: bool = True) -> str:
    attrs = f'Id="{user_id}" Name="{name}"'
    if with_email:
        attrs += f' Email="{email}"'
    return f"<GetCurrentUser><User {attrs} Credits='100' Trophy='9'/></GetCurrentUser>"


@pytest.fixture
def tokens_file(tmp_path, monkeypatch):
    """Redirige auth.TOKENS_FILE a un tokens.json aislado en tmp_path.

    _load_tokens lee TOKENS_FILE como global de pss_client.auth en cada
    llamada, así que parchear el atributo del módulo basta (cli usa la
    misma función importada de auth).
    """
    import pss_client.auth as auth_mod
    path = tmp_path / "tokens.json"
    monkeypatch.setattr(auth_mod, "TOKENS_FILE", str(path))
    return path


@pytest.fixture
def install_fake_http(monkeypatch):
    """Inyecta un FakeHttpClient como PssHttpClient del módulo cli."""
    created: list[FakeHttpClient] = []

    def _install(response_xml: str | None = None, exc: Exception | None = None) -> FakeHttpClient:
        fake = FakeHttpClient(response_xml=response_xml, exc=exc)
        created.append(fake)
        monkeypatch.setattr(cli_mod, "PssHttpClient", lambda: fake)
        return fake

    def _instantiations() -> int:
        return len(created)

    _install.count = _instantiations
    return _install


def _write_tokens(path, entries: dict) -> None:
    path.write_text(json.dumps(entries), encoding="utf-8")


def _valid_entry(access_token: str = "tok-abc123", user_id: int = 42,
                 saved_at: str = "2026-10-10T00:00:00") -> dict:
    return {
        "device_key": "dk-1",
        "refresh_token": "ref-1",
        "user_id": user_id,
        "saved_at": saved_at,
        "access_token": access_token,
    }


# --- 1. Sesión válida -------------------------------------------------------- #


def test_whoami_valid_session_prints_email_and_active(tokens_file, install_fake_http, capsys):
    _write_tokens(tokens_file, {"alice@example.com": _valid_entry()})
    fake = install_fake_http(response_xml=_user_xml())
    rc = cli_mod.main(["whoami"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Email:  alice@example.com" in out
    assert "Usuario: Alice (Id 42)" in out
    assert "Sesión: activa" in out
    # verificó de verdad contra la API, con el token persistido
    assert fake.calls and fake.calls[0][0] == "UserService/GetCurrentUser"
    assert fake.calls[0][2].get("accessToken") == "tok-abc123"
    assert fake.closed


def test_whoami_uses_api_email_and_falls_back_to_persisted(tokens_file, install_fake_http, capsys):
    # la API responde el email autoritativo aunque el guardado difiera
    _write_tokens(tokens_file, {"old@example.com": _valid_entry()})
    install_fake_http(response_xml=_user_xml(email="real@example.com"))
    rc = cli_mod.main(["whoami"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "real@example.com" in out
    assert "old@example.com" not in out


def test_whoami_email_fallback_when_api_omits_it(tokens_file, install_fake_http, capsys):
    # cuenta temporal sin Email en el XML → usa el email persistido
    _write_tokens(tokens_file, {"alice@example.com": _valid_entry()})
    install_fake_http(response_xml=_user_xml(with_email=False))
    rc = cli_mod.main(["whoami"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Email:  alice@example.com" in out


def test_whoami_json_output(tokens_file, install_fake_http, capsys):
    _write_tokens(tokens_file, {"alice@example.com": _valid_entry()})
    install_fake_http(response_xml=_user_xml())
    rc = cli_mod.main(["whoami", "--json"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    data = json.loads(out)
    assert data == {
        "email": "alice@example.com",
        "status": "active",
        "user_id": "42",
        "name": "Alice",
    }


def test_whoami_is_read_only_never_touches_tokens(tokens_file, install_fake_http, capsys):
    _write_tokens(tokens_file, {"alice@example.com": _valid_entry()})
    before = tokens_file.read_text(encoding="utf-8")
    install_fake_http(response_xml=_user_xml())
    assert cli_mod.main(["whoami"]) == 0
    capsys.readouterr()
    assert tokens_file.read_text(encoding="utf-8") == before


# --- 2. Sin credenciales ------------------------------------------------------ #


def test_whoami_without_session_prints_login_hint(tokens_file, capsys):
    # tokens.json ni existe
    rc = cli_mod.main(["whoami"])
    out = capsys.readouterr().out
    assert rc == cli_mod.EXIT_NOT_AUTHENTICATED
    assert rc != 0
    assert "No autenticado" in out
    assert "uv run python -m pss_client.api" in out


def test_whoami_with_empty_tokens_file_prints_login_hint(tokens_file, capsys):
    _write_tokens(tokens_file, {})
    rc = cli_mod.main(["whoami"])
    out = capsys.readouterr().out
    assert rc == cli_mod.EXIT_NOT_AUTHENTICATED
    assert "No autenticado" in out


def test_whoami_makes_no_http_call_without_session(tokens_file, install_fake_http, monkeypatch):
    boom = FakeHttpClient(exc=AssertionError("no debería haber llamada HTTP"))
    # parche defensivo: si alguien instanciara el cliente, el test explota
    monkeypatch.setattr(cli_mod, "PssHttpClient", lambda: boom)
    rc = cli_mod.main(["whoami"])
    assert rc == cli_mod.EXIT_NOT_AUTHENTICATED
    assert boom.calls == []


def test_whoami_unknown_email_lists_available_accounts(tokens_file, capsys):
    _write_tokens(tokens_file, {
        "alice@example.com": _valid_entry(saved_at="2026-10-09T00:00:00"),
        "bob@example.com": _valid_entry(user_id=7, saved_at="2026-10-10T00:00:00"),
    })
    rc = cli_mod.main(["whoami", "--email", "nobody@example.com"])
    out = capsys.readouterr().out
    assert rc == cli_mod.EXIT_NOT_AUTHENTICATED
    assert "nobody@example.com" in out
    assert "alice@example.com" in out
    assert "bob@example.com" in out


# --- 3. Token expirado -------------------------------------------------------- #


def test_whoami_expired_token_reports_expired_without_crash(tokens_file, install_fake_http, capsys):
    _write_tokens(tokens_file, {"alice@example.com": _valid_entry(access_token="tok-viejo")})
    install_fake_http(exc=PssApiError("Failed to authorize access token."))
    rc = cli_mod.main(["whoami"])
    out = capsys.readouterr().out
    assert rc == cli_mod.EXIT_SESSION_EXPIRED
    assert rc != 0
    assert "Email:  alice@example.com" in out
    assert "Sesión: expirada" in out
    assert "authorize access token" in out
    assert "No autenticado" in out  # pista de re-login


def test_whoami_expired_token_json_output(tokens_file, install_fake_http, capsys):
    _write_tokens(tokens_file, {"alice@example.com": _valid_entry()})
    install_fake_http(exc=PssApiError("Failed to authorize access token."))
    rc = cli_mod.main(["whoami", "--json"])
    data = json.loads(capsys.readouterr().out.strip())
    assert rc == cli_mod.EXIT_SESSION_EXPIRED
    assert data["email"] == "alice@example.com"
    assert data["status"] == "expired"


def test_whoami_refresh_only_entry_is_expired(tokens_file, install_fake_http, capsys):
    # tokens.json viejo: solo refreshToken, sin accessToken verificable
    entry = _valid_entry()
    entry.pop("access_token")
    _write_tokens(tokens_file, {"alice@example.com": entry})
    rc = cli_mod.main(["whoami"])
    out = capsys.readouterr().out
    assert rc == cli_mod.EXIT_SESSION_EXPIRED
    assert "Sesión: expirada" in out
    assert "no hay accessToken persistido" in out


# --- Otros errores (sin stack trace crudo) ----------------------------------- #


def test_whoami_api_error_exits_1(tokens_file, install_fake_http, capsys):
    _write_tokens(tokens_file, {"alice@example.com": _valid_entry()})
    install_fake_http(exc=PssApiError("Maintenance"))
    rc = cli_mod.main(["whoami"])
    out = capsys.readouterr().out
    assert rc == cli_mod.EXIT_ERROR
    assert "Error de la API de PSS: Maintenance" in out
    assert "Traceback" not in out


def test_whoami_network_error_exits_1(tokens_file, install_fake_http, capsys):
    _write_tokens(tokens_file, {"alice@example.com": _valid_entry()})
    install_fake_http(exc=aiohttp.ClientError("DNS failure"))
    rc = cli_mod.main(["whoami"])
    out = capsys.readouterr().out
    assert rc == cli_mod.EXIT_ERROR
    assert "Error de red" in out
    assert "Traceback" not in out


# --- Selección de cuenta ------------------------------------------------------- #


def test_whoami_picks_most_recent_by_default(tokens_file, install_fake_http, capsys):
    _write_tokens(tokens_file, {
        "alice@example.com": _valid_entry(user_id=42, saved_at="2026-10-09T00:00:00"),
        "bob@example.com": _valid_entry(access_token="tok-bob", user_id=7,
                                        saved_at="2026-10-10T00:00:00"),
    })
    fake = install_fake_http(response_xml=_user_xml(email="bob@example.com",
                                                   name="Bob", user_id="7"))
    rc = cli_mod.main(["whoami"])
    out = capsys.readouterr().out
    assert rc == 0
    assert fake.calls[0][2].get("accessToken") == "tok-bob"
    assert "bob@example.com" in out


def test_whoami_email_flag_selects_account(tokens_file, install_fake_http, capsys):
    _write_tokens(tokens_file, {
        "alice@example.com": _valid_entry(user_id=42, saved_at="2026-10-09T00:00:00"),
        "bob@example.com": _valid_entry(access_token="tok-bob", user_id=7,
                                        saved_at="2026-10-10T00:00:00"),
    })
    fake = install_fake_http(response_xml=_user_xml(email="alice@example.com",
                                                   name="Alice", user_id="42"))
    rc = cli_mod.main(["whoami", "--email", "Alice@Example.com"])  # case-insensitive
    out = capsys.readouterr().out
    assert rc == 0
    assert fake.calls[0][2].get("accessToken") == "tok-abc123"
    assert "alice@example.com" in out


# --- Registro del subcomando ---------------------------------------------------- #


def test_cli_without_subcommand_shows_help(capsys):
    rc = cli_mod.main([])
    out = capsys.readouterr().out
    assert rc == cli_mod.EXIT_NOT_AUTHENTICATED
    assert "usage:" in out
    assert "whoami" in out


def test_parser_registers_whoami_with_flags():
    parser = cli_mod._build_parser()
    args = parser.parse_args(["whoami", "--email", "a@b.c", "--json", "--debug"])
    assert args.command == "whoami"
    assert args.email == "a@b.c"
    assert args.json is True
    assert args.debug is True
    assert args.func is cli_mod._cmd_whoami


# --- E2E en subprocess (como lo invoca el usuario) ----------------------------- #


def test_whoami_subprocess_end_to_end(tmp_path):
    """Smoke real: `python -m pss_client.cli whoami` en un HOME limpio.

    Ejecuta el entrypoint de verdad en un proceso aparte con HOME aislado
    (sin ~/.pss-client/tokens.json) y valida mensaje claro + exit code.
    """
    import os
    import subprocess
    import sys

    env = dict(os.environ, HOME=str(tmp_path))
    # sin red: un HOME sin tokens nunca llega a hacer HTTP
    proc = subprocess.run(
        [sys.executable, "-m", "pss_client.cli", "whoami"],
        capture_output=True, text=True, env=env, cwd=str(
            __import__("pathlib").Path(__file__).resolve().parents[1]),
        timeout=60,
    )
    assert proc.returncode == cli_mod.EXIT_NOT_AUTHENTICATED, proc.stderr
    assert "No autenticado" in proc.stdout
    assert "Traceback" not in proc.stderr
    assert "Traceback" not in proc.stdout


def test_whoami_subprocess_help(tmp_path):
    import os
    import subprocess
    import sys

    env = dict(os.environ, HOME=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, "-m", "pss_client.cli", "--help"],
        capture_output=True, text=True, env=env, cwd=str(
            __import__("pathlib").Path(__file__).resolve().parents[1]),
        timeout=60,
    )
    assert proc.returncode == 0
    assert "whoami" in proc.stdout

    # los flags del subcomando están en la ayuda del propio subcomando
    proc_sub = subprocess.run(
        [sys.executable, "-m", "pss_client.cli", "whoami", "--help"],
        capture_output=True, text=True, env=env, cwd=str(
            __import__("pathlib").Path(__file__).resolve().parents[1]),
        timeout=60,
    )
    assert proc_sub.returncode == 0
    assert "whoami" in proc_sub.stdout
    assert "--email" in proc_sub.stdout
    assert "--json" in proc_sub.stdout
