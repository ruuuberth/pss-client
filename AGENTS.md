# AGENTS.md — pss-client

Cliente no oficial de Pixel Starships: backend Python (`aiohttp` + XML), gateway FastAPI y dashboard de un solo HTML. Habla con la API de producción y con cuentas reales; la verificación habitual debe ser offline.

## Entorno y arranque

- Python `>=3.10` según `pyproject.toml`; CI prueba 3.10, 3.11 y 3.12.
- `uv sync --extra dev` — instala runtime + pytest en el entorno local.
- `python -m pip install -e ".[dev]"` — instalación editable con pytest usada por CI.
- `uv run python -m pss_client.api` — sirve API y dashboard en `0.0.0.0:8000`; el frontend no tiene build.
- `PSS_CHECKSUM_KEY` y `PSS_SAVY_CHECKSUM` son opcionales, tienen fallback embebido y se leen al importar `pss_client.auth`.
- `uv.lock` está ignorado por git; `pyproject.toml` es la fuente de dependencias.

## Verificación

- `uv run python -m pytest -q` — suite local completa, sin red.
- `python -m pytest -q` — mismo comando en CI; `testpaths = ["tests"]` recoge también los tests de nave y frontend.
- `gitleaks detect --no-banner --source . --config .gitleaks.toml` — escaneo local de secretos documentado en `.gitleaks.toml`.
- `uv run python test_auth.py` — smoke contra producción: crea una cuenta web temporal con `DeviceLogin17`; ejecútalo solo cuando se busque esa verificación en vivo.
- CI corre pytest + gitleaks en PRs a `develop`. No hay formatter, linter ni type checker configurados.

## Diseño y convenciones

- `transport.py`: cliente HTTP/XML compartido. PSS devuelve HTTP 200 incluso en errores; `errorMessage` se convierte en `PssApiError`. Conserva el caso especial `errorCode` + `<User>` como respuesta estructurada.
- `auth.py`: login de 3 pasos, checksums, refresh y persistencia. `api.py`: sesiones en memoria, restauración, heartbeats y serialización REST. `ship.py`/`game.py`: servicios y dataclasses; `designs.py`: catálogos y stats.
- Identificadores Python en inglés y `snake_case`; atributos XML con capitalización exacta (`BattleCharacterHp`, `CharacterName`). Los parsers usan dataclasses con `from_xml`; los numéricos vacíos se normalizan con `or 0`.
- Comentarios, docstrings, UI y nombres de tests están principalmente en español.
- Tests unitarios sin red: transporte fake, `asyncio.run` y XML fijo. Redirige `auth_module.TOKENS_FILE` a `tmp_path` con `monkeypatch`, como en `tests/test_auth_unit.py`, para aislar `~/.pss-client`.
- Salvo `/` y `/health`, los endpoints del gateway son POST JSON con `session_token`; sesión inválida devuelve 401 estructurado y errores PSS 400/500 con `detail`. Al cambiar el contrato, actualiza `docs/API.md`.
- Frontend vanilla en `frontend/index.html`: sesión y catálogo en `localStorage`, colores mediante variables de `:root` y filas de `<tbody>` mediante `row()`; `test_frontend_html.py` comprueba estas dos últimas reglas.
- `develop` es la rama base. Los commits usan prefijos `feat:`, `fix:`, `docs:`, `ci:` y suelen referenciar issues (`feat: issue #3 — …`).
- Consulta `docs/REVERSE-ENGINEERING.md` antes de cambiar checksums, fórmulas de stats o supuestos sobre mecánicas del juego.

## Pitfalls específicos de PSS

- Arrancar el gateway restaura tokens de `~/.pss-client/tokens.json` y reactiva heartbeats cada 60 s; login/heartbeat marcan la cuenta Online. Los endpoints `collect-resources` y `collect-daily-reward` modifican la cuenta real.
- `pss-client.log` se crea en la raíz con DEBUG y fragmentos de XML; los tokens persistidos y ese log contienen datos sensibles. El log está ignorado por git.
- El accessToken dura unos 52 min y se renueva a los 45; el `session_token` del navegador debe permanecer estable entre renovaciones y reinicios.
- `ListAllStaticDesigns`/`ListAllDynamicDesigns` devuelven `base64(gzip(XML))`, no XML directo. Hay que obtener primero las versiones con `GetLatestVersion4`: versiones en 0 producen un catálogo vacío. El catálogo estático pesa unos 13.5 MB y se cachea por proceso.
- `Shield=0` fuera de batalla es normal; la protección persistente es `ImmunityDate` (UTC) y el máximo se deriva de salas Shield. `Ship.Hp` representa daño pendiente de reparar.
