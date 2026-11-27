# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Unofficial Pixel Starships client: a Python backend (`pss_client/`) that talks to the real game API, a FastAPI gateway (`pss_client/api.py`), and a single-file vanilla dashboard (`frontend/index.html`). It talks to **production and to real user accounts** — some endpoints mutate the account. Prefer offline verification.

## Commands

```bash
uv sync --extra dev              # install runtime + pytest
python -m pip install -e ".[dev]"   # editable install used by CI

uv run python -m pss_client.api  # gateway + dashboard on http://localhost:8000 (0.0.0.0)

uv run python -m pytest -q       # full offline suite (26 tests, no network)
uv run python -m pytest tests/test_auth_unit.py -q            # single file
uv run python -m pytest tests/test_auth_unit.py::test_create_checksum_formula_exacta -q  # single test

gitleaks detect --no-banner --source . --config .gitleaks.toml   # local secret scan

uv run python test_auth.py       # SMOKE TEST AGAINST PRODUCTION: creates a temp web account via
                                 # DeviceLogin17. Only run when live verification is the goal.
```

Python is `>=3.10` (`pyproject.toml`); CI checks 3.10/3.11/3.12. No formatter, linter or type checker is configured — CI is pytest + gitleaks only (`.github/workflows/ci.yml`, runs on PRs targeting `develop`). The frontend has no build step. `uv.lock` is git-ignored; `pyproject.toml` is the dependency source.

## Architecture

```
frontend/index.html          dashboard (vanilla JS, session + design catalog in localStorage)
        │  fetch JSON
        ▼
pss_client/api.py            FastAPI gateway on :8000 (in-memory sessions, background heartbeat)
        │  aiohttp, XML
        ▼
api.pixelstarships.com       UserService / ShipService / DesignService / TaskService / RoomService / ...
```

**Request flow.** `frontend/index.html` → POST JSON with `session_token` → `api.py` endpoint → service class (`ship.PssShipService`, `game.PssGameService`, `designs.get_design_catalog`) → `transport.PssHttpClient` → XML back. `transport.py` is the shared HTTP/XML layer.

**Module responsibilities** (each is small; keep it that way):
- `transport.py` — HTTP + XML parsing, `PssApiError`.
- `auth.py` — the 3-step login, checksums, token persistence, `session_token` derivation.
- `ship.py` / `game.py` — service classes and their `from_xml` dataclasses.
- `designs.py` — design catalog download + the disassembled game formulas (`stat_for_level`, `max_hp_for`).
- `api.py` — sessions, restore, heartbeats, REST serialization.

**Session lifecycle** (`auth.py` + `api.py`) — the part that spans files:
- `~/.pss-client/tokens.json` maps email → `{device_key, refresh_token, user_id, saved_at, access_token}`. Startup `_restore_sessions()` re-registers any account with a persisted `access_token` and restarts its heartbeat, so the browser does not re-login after a server restart. `saved_at` is the real token emission time.
- Browser `session_token` is **stable per account**: `sess_{user_id}_{uuid5(NAMESPACE_URL, "pss-client:session:{email}")}` (`_session_token_for`). It must survive accessToken renewals and restarts.
- accessToken expires at ~52 min and is renewed proactively at 45 min (measured from `saved_at`, not loop start) and reactively on any endpoint that fails authorization.
- `_heartbeat_loop` sends `HeartBeat4` every 60 s per session, which keeps the account `Online` server-side.
- Endpoint contract (all except `/` and `/health` are POST JSON with `session_token`): invalid session → structured 401 with `detail.reason == "session_expired"`; PSS errors → 400/500 with `detail`. If you change the contract, update `docs/API.md`.

**Design catalog** (`designs.py`): `GetLatestVersion4` → `ListAllStaticDesigns` (~13.5 MB) → `ListAllDynamicDesigns`. The `ListAll*` responses are **`base64(gzip(XML))`**, not XML, and with versions set to 0 the API returns an empty catalog. Cached per process in `_DESIGN_CACHE`; the frontend also caches it in `localStorage` (`pss_designs`).

## Conventions

- Python identifiers are English `snake_case`; XML attributes keep the game's exact capitalization (`BattleCharacterHp`, `CharacterName`, not `Name`). Parsers are dataclasses with `from_xml`; empty numerics normalize with `or 0`.
- Comments, docstrings, UI text and test names are mainly in **Spanish** — match that when editing in place.
- Derived stats come from the disassembled client, not guesses. Before changing checksums, stat formulas or game-mechanics assumptions, read `docs/REVERSE-ENGINEERING.md`.
- Frontend (`frontend/index.html`): colors must come from the `:root` CSS variables and `<tbody>` rows must be built with the `row()` helper. `tests/test_frontend_html.py` enforces both — it is a static regex check on the source, so keep the source patterns it greps for.
- Commits use Conventional Commits prefixes (`feat:`, `fix:`, `docs:`, `ci:`) and often reference issues (`feat: issue #7 — …`). `develop` is the base branch.

## Testing

Tests are offline: fake transports, `asyncio.run`, fixed XML. `auth_module.TOKENS_FILE` is redirected to `tmp_path` via `monkeypatch` so no test touches `~/.pss-client` — follow that pattern for anything that would persist tokens. `tests/test_auth_unit.py` also has a `FakeHttp` that records calls, which is how tests assert outbound request params (e.g. checksums).

Notable contract tests: `tests/test_ship_api.py` checks the `/ship/info` enrichment (`max_shield = Σ capacity × 100`, base HP kept separate from the gym bonus); `tests/test_ship.py` checks `max_hp_for = round(base) * (100 + bonus) // 100`.

## Pitfalls

- Starting the gateway restores persisted tokens and reactivates heartbeats every 60 s; login/heartbeat mark the account `Online`. `/game/collect-resources` and `/game/collect-daily-reward` **write to the real account**.
- `pss-client.log` at the repo root is DEBUG with XML fragments; it and `~/.pss-client/tokens.json` contain sensitive data. The log is git-ignored.
- PSS returns HTTP 200 even on errors: `errorMessage` lives in the XML and is converted to `PssApiError` in `transport.py`. Keep the special case where `errorCode` **plus a `<User>` child** is a structured response (e.g. `linked_account`), not a flat error.
- PSS game semantics that are easy to misread: `Shield=0` outside battle is normal (persistent protection is `ImmunityDate`, PvP immunity, in UTC; max shield is derived from Shield rooms). `Ship.Hp` is pending repair damage. A character's max HP is catalog-derived and includes the gym bonus.