"""Tests de validación del HTML del frontend (issue #12).

Los tests estáticos (sin dependencias) validan el source de
frontend/index.html: las filas Escudo/Inmunidad del Resumen deben usar
el mismo markup <tr> del helper row() que el resto de filas, y los
colores deben venir de variables CSS definidas en :root.

La validación de renderizado real en navegador se hace aparte con
Node+jsdom via scripts/render_check.js (ver README del repo).

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
