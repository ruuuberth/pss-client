"""Tests de validación del HTML del frontend (issues #12 y #4).

Tests estáticos (sin dependencias) sobre el source de frontend/index.html:
las filas Escudo/Inmunidad del Resumen deben usar el mismo markup <tr>
del helper row() que el resto de filas, los colores deben venir de
variables CSS definidas en :root, y el contador de inmunidad (issue #4)
debe correr en el cliente con alerta de vencimiento (<1h / vencida).

La verificación visual en navegador (DOM renderizado, estilos calculados
y edge cases) se hace manualmente con el archivo servido localmente;
ver la sección de verificación del PR que cierra el issue #12.

Uso:
    python -m pytest test_frontend_html.py -q
"""

from pathlib import Path
import re

import pytest

HTML = Path(__file__).parent / "frontend" / "index.html"


@pytest.fixture(scope="module")
def source():
    return HTML.read_text(encoding="utf-8")


def _fn_body(src, name):
    """Devuelve el cuerpo de function <name>() {...} del script inline."""
    m = re.search(rf"function {name}\((.*?)\) \{{(.*?)\n\}}", src, re.S)
    assert m, f"No se encontró function {name}() en index.html"
    return m.group(2)


# ---------- AC1: markup <tr> idéntico al resto de filas ----------

def test_shieldrow_devuelve_row_helper(source):
    """shieldRow() devuelve row(...) — <tr><td> — en todas sus ramas."""
    body = _fn_body(source, "shieldRow")
    returns = re.findall(r"return ([^;]+);", body)
    assert returns, "shieldRow() no tiene returns"
    for r in returns:
        r = r.strip()
        assert r.startswith("row("), (
            f"shieldRow() retorna '{r}' — debe usar el helper row() "
            "(mismo markup <tr> que las demás filas)"
        )


def test_immunityrow_devuelve_row_helper(source):
    """immunityRow() devuelve row(...) en todas sus ramas (o '' si null)."""
    body = _fn_body(source, "immunityRow")
    returns = re.findall(r"return ([^;]+);", body)
    assert returns, "immunityRow() no tiene returns"
    for r in returns:
        r = r.strip()
        assert r == "''" or r.startswith("row("), (
            f"immunityRow() retorna '{r}' — debe usar row() (o '' si no hay inmunidad)"
        )


def test_source_sin_div_row_dentro_de_funciones_de_fila(source):
    """Ninguna función de fila genera <div class=\"row\">."""
    assert '<div class="row">' not in source, (
        'Queda <div class="row"> en el source: markup inválido dentro de <tbody>'
    )


# ---------- AC2: colores vía variables CSS de :root ----------

def test_source_sin_muted_foreground(source):
    """var(--muted-foreground) no está definida en :root: no debe usarse."""
    assert "var(--muted-foreground)" not in source, (
        "var(--muted-foreground) se usa pero NO existe en :root "
        "(hereda --text, blanco brillante). Usar var(--text-3)."
    )


def test_source_sin_verde_hardcodeado(source):
    """#22c55e hardcodeado fuera: la fila de inmunidad usa var(--success)."""
    assert "#22c55e" not in source, (
        "Color #22c55e hardcodeado en vez de var(--success) (#10b981)"
    )


def test_root_define_variables_usadas(source):
    """:root define las variables que las filas corregidas usan."""
    assert re.search(r"--text-3\s*:\s*#[0-9a-fA-F]{6}", source), (
        "--text-3 no está definida en :root"
    )
    assert re.search(r"--success\s*:\s*#[0-9a-fA-F]{6}", source), (
        "--success no está definida en :root"
    )


def test_colores_de_las_filas_usan_variables_definidas(source):
    """Todo var(--X) usado en estilos inline de las filas existe en :root."""
    m = re.search(r":root\s*\{(.*?)\n\}", source, re.S)
    assert m, "No se encontró el bloque :root"
    defined = set(re.findall(r"(--[\w-]+)\s*:", m.group(1)))
    usados = set(re.findall(r"var\((--[\w-]+)\)", source))
    faltan = usados - defined
    assert not faltan, (
        f"Variables CSS usadas pero no definidas en :root: {sorted(faltan)}"
    )


# ---------- Issue #4: contador de inmunidad en vivo + alerta ---------- #

def test_immunityrow_calcula_countdown_en_cliente(source):
    """El countdown se computa en el cliente (no depende del backend)."""
    body = _fn_body(source, "immunityRow")
    assert "Date.now()" in body, (
        "immunityRow() no calcula el restante contra Date.now() — "
        "el contador debe correr en el cliente (issue #4)"
    )
    assert "padStart" in body, (
        "immunityRow() no formatea el countdown (se esperaba padStart hh:mm:ss)"
    )


def test_parse_utc_no_usa_date_naive(source):
    """ImmunityDate viaja en UTC: el parseo debe ser Date.UTC, no Date() naive."""
    body = _fn_body(source, "parseUtc")
    assert "Date.UTC(" in body, (
        "parseUtc() debe construir la fecha con Date.UTC() — la API manda "
        "ImmunityDate en UTC sin zona (issue #4)"
    )


def test_alerta_menor_1h_con_pill_warn(source):
    """<1h: fila en color de alerta (--warn) + pill '⚠ <1h'."""
    body = _fn_body(source, "immunityRow")
    assert "3600" in body, "immunityRow() no discrimina el umbral de 1h"
    assert "var(--warn)" in body, "<1h debe usar var(--warn), no un color hardcodeado"
    assert "pill warn" in body, "<1h debe añadir la pill de alerta '⚠ <1h'"


def test_inmunidad_vencida_en_danger(source):
    """Ya vencida (remainS=0): texto 'vencida' con var(--danger)."""
    body = _fn_body(source, "immunityRow")
    assert "vencida" in body and "var(--danger)" in body, (
        "immunityRow() no muestra el estado vencida con var(--danger)"
    )


def test_tick_redibuja_solo_la_celda(source):
    """El tick de 1s actualiza la celda del contador, no toda la tabla."""
    body = _fn_body(source, "tickImmunity")
    assert "renderAll" not in body, (
        "tickImmunity() llama a renderAll(): re-renderizaría todo el Resumen "
        "cada segundo — debe tocar solo la celda del contador"
    )
    assert "api(" not in body, (
        "tickImmunity() llama a la API cada segundo — martillar /ship/info "
        "1 req/s; el re-sync es de 60s en startImmunityClock()"
    )
    assert "cell.innerHTML" in body, (
        "tickImmunity() debe escribir en la celda del contador (cell.innerHTML)"
    )


def test_reloj_cada_1s_y_resync_60s(source):
    """Tick de 1s + re-sync de /ship/info cada 60s (alineado al heartbeat)."""
    body = _fn_body(source, "startImmunityClock")
    assert "setInterval(tickImmunity, 1000)" in body, (
        "startImmunityClock() no arranca el tick de 1s"
    )
    assert "60000" in body and "loadShip" in body, (
        "startImmunityClock() no programa el re-sync de /ship/info cada 60s"
    )


def test_logout_detiene_el_reloj(source):
    """doLogout() detiene el contador y el re-sync (sin timers huérfanos)."""
    body = _fn_body(source, "doLogout")
    assert "stopImmunityClock()" in body, (
        "doLogout() no detiene el reloj de inmunidad: dejaría corriendo "
        "el tick y el re-sync tras cerrar sesión"
    )


# ---------- Review PR #14: frontera exacta de vencida (remainS <= 0) ---------- #

def test_immunityrow_frontera_vencida_remain_le_0(source):
    """La condición de vencida de immunityRow() es remainS <= 0 (frontera exacta).

    La frontera debe ser la misma que usa tickImmunity(): <= 0. Con el clamp
    de Math.max(0, ...) ambos operadores se comportan hoy igual, pero === 0
    solo describía el instante exacto del vencimiento y quedaba a un paso de
    mostrar una cuenta 00:00:00 vencida si el clamp cambiaba.
    """
    body = _fn_body(source, "immunityRow")
    m = re.search(r"if \(remainS\s*(<=|===|==|<)\s*0\)", body)
    assert m, "immunityRow() no tiene condición de vencida sobre remainS"
    assert m.group(1) == "<=", (
        f"immunityRow() marca vencida con remainS {m.group(1)} 0 — el tick usa "
        "<= 0 y se autocorrige en 1s: el render inicial debe usar la misma "
        "frontera exacta (issue del review del PR #14)"
    )


def test_immunityrow_y_tick_misma_condicion_vencida(source):
    """immunityRow() y tickImmunity() comparten la MISMA condición de vencida."""
    def cond(body):
        m = re.search(r"if \(remainS\s*(<=|===|==|<)\s*0\)", body)
        assert m, "no hay condición de vencida sobre remainS"
        return m.group(1)
    row_cond = cond(_fn_body(source, "immunityRow"))
    tick_cond = cond(_fn_body(source, "tickImmunity"))
    assert row_cond == tick_cond == "<=", (
        "immunityRow() y tickImmunity() deben usar la misma frontera de vencida "
        f"(remainS <= 0): hoy divergen (immunityRow {row_cond}, tick {tick_cond}) "
        "y el render inicial puede mostrar una cuenta 00:00:00 ya vencida"
    )


def test_tick_condicion_vencida_no_se_cambia(source):
    """Guard: el tick sigue siendo la referencia de la frontera (remainS <= 0)."""
    body = _fn_body(source, "tickImmunity")
    assert re.search(r"if \(remainS\s*<=\s*0\)", body), (
        "tickImmunity() ya no usa remainS <= 0: es la referencia de la frontera "
        "de vencida; si cambió a propósito, revisar también immunityRow() y "
        "test_immunityrow_y_tick_misma_condicion_vencida"
    )
