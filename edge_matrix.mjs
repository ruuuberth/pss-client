/**
 * Edge matrix PR #14 — frontera remainS <= 0 de immunityRow (task t_81c96a93).
 *
 * Extrae las funciones REALES (parseUtc / immunityRow / tickImmunity) del HTML
 * del árbol de trabajo y las ejecuta en Node con el reloj congelado y alineado
 * al segundo (misma metodología del review del PR #14, que dio 21/21).
 *
 * Uso: node edge_matrix.mjs            → 21/21 esperados (exit 0)
 */
import { readFileSync } from "node:fs";

const html = readFileSync(new URL("./frontend/index.html", import.meta.url), "utf8");

// --- extraer el cuerpo de function name(...) {...} del script inline ---
function fnBody(name) {
  const m = html.match(new RegExp(`function ${name}\\([^)]*\\) \\{([\\s\\S]*?)\\n\\}`));
  if (!m) throw new Error(`No se encontró function ${name}()`);
  return m[1];
}

const FNS =
  `\nfunction parseUtc(s) {${fnBody("parseUtc")}\n}` +
  `\nfunction immunityRow() {${fnBody("immunityRow")}\n}` +
  `\nfunction tickImmunity() {${fnBody("tickImmunity")}\n}`;

// --- entorno mínimo ---
// rowsExpr: expresión que devuelve las filas del Resumen para tickImmunity().
//   null  → sin document (immunityRow solo compone el string de row())
//   "[]"  → DOM sin la fila (tick no-op)
const ROW = `{ textContent: "Protección (inmunidad PvP)", querySelector: () => __cell }`;
const sandbox = (imJson, rowsExpr) => `
  const shipData = { immunity: ${imJson} };
  const row = (k, v) => "<tr><td class='sub2'>" + k + "</td><td class='strong'>" + v + "</td></tr>";
  ${
    rowsExpr !== null
      ? `const __cell = { innerHTML: "" };
  const document = { querySelectorAll: (sel) => (${rowsExpr}) };`
      : ""
  }
`;
const ROWS_PRESENT = `[${ROW}]`;

function runWithFrozenClock(iso, code) {
  // Reloj congelado al ms exacto del ISO destino (UTC). immunityRow y
  // tickImmunity solo comparan contra Date.now(); parseUtc usa Date.UTC
  // (estático), así que congelar Date.now basta.
  const epoch = Date.parse(iso + "Z");
  if (Number.isNaN(epoch)) throw new Error("ISO inválido: " + iso);
  // eslint-disable-next-line no-new-func
  return new Function(`Date.now = () => ${epoch};\n` + code)();
}

let n = 0;
const results = [];
function check(desc, cond) {
  n += 1;
  results.push(`${cond ? "PASS" : "FAIL"} ${String(n).padStart(2)} — ${desc}`);
}

const untilOf = (offsetSec, iso) =>
  new Date(Date.parse(iso + "Z") + offsetSec * 1000).toISOString().slice(0, 19);

// ============================================================
// Grupo A — immunityRow() render inicial: frontera <1h exacta
// ============================================================
{
  const NOW = "2026-10-12T12:00:00";
  const cases = [
    [3600, "verde sin pill (01:00:00)", "var(--success)", false, "01:00:00"],
    [3599, "warn + pill (00:59:59)", "var(--warn)", true, "00:59:59"],
    [1, "warn + pill (00:00:01)", "var(--warn)", true, "00:00:01"],
  ];
  for (const [off, desc, color, pill, cd] of cases) {
    const im = JSON.stringify({ active: true, until: untilOf(off, NOW) });
    const out = runWithFrozenClock(NOW, sandbox(im, null) + FNS + "\nreturn immunityRow()");
    check(
      `A: render ${off}s → ${desc}`,
      out.includes(color) &&
        out.includes(cd) &&
        out.includes("vence en") &&
        (pill ? out.includes("pill warn") : !out.includes("pill warn")) &&
        !out.includes("vencida")
    );
  }
}

// ============================================================
// Grupo B — immunityRow() render inicial: frontera de vencida (<= 0)
// ============================================================
{
  const NOW = "2026-10-12T12:00:00";
  for (const [off, desc] of [[0, "until == ahora"], [-1, "until ya pasada (delta -1s)"]]) {
    const im = JSON.stringify({ active: true, until: untilOf(off, NOW) });
    const out = runWithFrozenClock(NOW, sandbox(im, null) + FNS + "\nreturn immunityRow()");
    check(`B: render ${desc} → var(--danger) '⚠ vencida'`, out.includes("var(--danger)") && out.includes("vencida"));
  }
  // Sin fecha parseable: no fabricar countdown (issue #4). Con until basura el
  // valor se muestra tal cual ("activa hasta garbage"); el fallback
  // '(fecha desconocida)' aplica cuando no hay fecha en absoluto.
  {
    const im1 = JSON.stringify({ active: true, until: "garbage" });
    const out1 = runWithFrozenClock(NOW, sandbox(im1, null) + FNS + "\nreturn immunityRow()");
    const im2 = JSON.stringify({ active: true, until: null });
    const out2 = runWithFrozenClock(NOW, sandbox(im2, null) + FNS + "\nreturn immunityRow()");
    check(
      "B: render until no parseable → sin countdown inventado; sin fecha → '(fecha desconocida)'",
      out1.includes("var(--success)") &&
        out1.includes("activa hasta") &&
        !out1.includes("vence en") &&
        out2.includes("activa hasta (fecha desconocida)") &&
        !out2.includes("vence en")
    );
  }
}

// ============================================================
// Grupo C — tickImmunity(): la referencia (remainS <= 0 ya en el PR)
// ============================================================
{
  const NOW = "2026-10-12T12:00:00";
  for (const [off, desc] of [[0, "until == ahora"], [-1, "until ya pasada"]]) {
    const im = JSON.stringify({ active: true, until: untilOf(off, NOW) });
    const out = runWithFrozenClock(NOW, sandbox(im, ROWS_PRESENT) + FNS + "\ntickImmunity(); return __cell.innerHTML");
    check(`C: tick ${desc} → celda var(--danger) '⚠ vencida'`, out.includes("var(--danger)") && out.includes("vencida"));
  }
  {
    const im = JSON.stringify({ active: true, until: untilOf(3599, NOW) });
    const out = runWithFrozenClock(NOW, sandbox(im, ROWS_PRESENT) + FNS + "\ntickImmunity(); return __cell.innerHTML");
    check("C: tick 3599s → warn + pill '⚠ <1h' + 00:59:59", out.includes("var(--warn)") && out.includes("pill warn") && out.includes("00:59:59"));
  }
  {
    const im = JSON.stringify({ active: true, until: untilOf(3600, NOW) });
    const out = runWithFrozenClock(NOW, sandbox(im, ROWS_PRESENT) + FNS + "\ntickImmunity(); return __cell.innerHTML");
    check("C: tick 3600s → verde sin pill (01:00:00)", out.includes("var(--success)") && !out.includes("pill warn") && out.includes("01:00:00"));
  }
  // Sin fila en el DOM → no-op sin excepción
  {
    const im = JSON.stringify({ active: true, until: untilOf(60, NOW) });
    const out = runWithFrozenClock(NOW, sandbox(im, "[]") + FNS + "\ntickImmunity(); return 'no-op ok'");
    check("C: tick con fila ausente en DOM → no-op sin excepción", out === "no-op ok");
  }
  // Sin datos → detiene timers limpio
  {
    const code =
      sandbox("null", null) + FNS +
      "\nlet stopped = 0; function stopImmunityClock() { stopped++; }" +
      "\ntickImmunity(); return stopped === 1 ? 'detenido' : 'NO detenido'";
    const out = runWithFrozenClock(NOW, code);
    check("C: tick sin datos → stopImmunityClock() llamado", out === "detenido");
  }
}

// ============================================================
// Grupo D — parseUtc (UTC, no zona del navegador)
// ============================================================
{
  const NOW = "2026-10-12T12:00:00";
  const code =
    sandbox("null", null) + FNS +
    "\nreturn JSON.stringify([parseUtc('2026-10-12T02:48:43').getTime(), parseUtc(null), parseUtc(''), parseUtc('garbage')])";
  const out = JSON.parse(runWithFrozenClock(NOW, code));
  check("D: parseUtc interpreta la fecha en UTC (epoch exacto)", out[0] === Date.parse("2026-10-12T02:48:43Z"));
  check("D: parseUtc(null) → null", out[1] === null);
  check("D: parseUtc('') → null", out[2] === null);
  check("D: parseUtc('garbage') → null", out[3] === null);
}

// ============================================================
// Grupo E — coherencia immunityRow ↔ tickImmunity (issue de esta card)
// ============================================================
{
  const NOW = "2026-10-12T12:00:00";
  // Fecha ya pasada (delta -1s): AMBAS rutas deben mostrar "vencida" en el
  // MISMO segundo — esta es la divergencia que reportó el review del PR #14.
  const im = JSON.stringify({ active: true, until: untilOf(-1, NOW) });
  const rowOut = runWithFrozenClock(NOW, sandbox(im, null) + FNS + "\nreturn immunityRow()");
  const tickOut = runWithFrozenClock(NOW, sandbox(im, ROWS_PRESENT) + FNS + "\ntickImmunity(); return __cell.innerHTML");
  check("E: delta -1s → immunityRow muestra 'vencida'", rowOut.includes("vencida"));
  check("E: delta -1s → tickImmunity muestra 'vencida'", tickOut.includes("vencida"));
  check("E: delta -1s → ninguna ruta muestra cuenta 00:00:00", !rowOut.includes("00:00:00") && !tickOut.includes("00:00:00"));

  // La condición textual de ambas funciones es la misma
  const condOf = (name) => {
    const m = fnBody(name).match(/if \(remainS\s*(<=|===|==|<)\s*0\)/);
    return m ? m[1] : null;
  };
  check("E: immunityRow usa remainS <= 0 (no === 0)", condOf("immunityRow") === "<=");
  check("E: tickImmunity usa remainS <= 0", condOf("tickImmunity") === "<=");
}

console.log(results.join("\n"));
const fails = results.filter((r) => r.startsWith("FAIL")).length;
console.log(`\n${n - fails}/${n} checks passed`);
process.exit(fails ? 1 : 0);
