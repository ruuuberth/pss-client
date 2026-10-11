"""Fixtures compartidos de la suite de tests de pss-client.

Aísla cada test del estado global del proceso:
- tokens.json real (~/.pss-client/tokens.json) -> copia temporal por test
- sesiones en memoria (pss_client.api.active_sessions) -> vaciadas por test
- heartbeats en background desactivados (no son objeto de la suite)

Todo test que necesite la API de PSS debe parchear la capa HTTP (ver el
FakePssApi de tests/test_session_renewal_regression.py): la suite NUNCA
toca la red real, para que sea determinista en CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Hacer importable pss_client sin instalación (p.ej. `uv run --no-project`).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from pss_client import api as pss_api
from pss_client import auth as pss_auth


@pytest.fixture
def anyio_backend():
    """Backend asyncio para los tests async (plugin pytest de anyio)."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _aislar_estado_global(monkeypatch, tmp_path):
    """tokens.json temporal + estado de sesiones limpio en cada test."""
    monkeypatch.setattr(pss_auth, "TOKENS_FILE", str(tmp_path / "tokens.json"))
    # /auth/login arranca un heartbeat asyncio por sesión; en la suite es
    # ruido (tareas pendientes al cerrar el loop) -> no-op.
    monkeypatch.setattr(pss_api, "_start_heartbeat", lambda *_a, **_k: None)

    sesiones = dict(pss_api.active_sessions)
    tareas = dict(pss_api._heartbeat_tasks)
    pss_api.active_sessions.clear()
    pss_api._heartbeat_tasks.clear()
    yield
    pss_api.active_sessions.clear()
    pss_api.active_sessions.update(sesiones)
    pss_api._heartbeat_tasks.clear()
    pss_api._heartbeat_tasks.update(tareas)
